"""API / BFF Service (doc Section 5.1, 5.3). Serves the dashboard: JWT auth,
live cost reads, the anomaly/action feeds, policy management, and the
human-in-the-loop approve/rollback controls (FR-10/FR-11).

Write operations are gated on the `sre` role (FR-14): a Finance/FinOps user
can see cost and incident data but cannot create policies or trigger
remediation. RBAC is enforced here in the backend, non-negotiably, and
again in the Phase 5 frontend for UX (doc Section 6.2).

This service never touches the cluster itself. Approve and rollback publish
onto the remediation queue for the Remediation Executor to act on, so
Kubernetes write credentials stay confined to that one component (NFR-4).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncio

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from hypertrace_common.db import make_engine
from hypertrace_common.messaging import ROUTING_KEY_REMEDIATION, RabbitMQClient
from hypertrace_common.schemas import RemediationAction
from hypertrace_common.tables import actions_log, anomalies, cost_events, policies

from . import config
from .alert_stream import AlertBroadcaster
from .users import authenticate_user

app = FastAPI(title="HyperTrace API", version="0.1.0")
engine = make_engine()
publish_mq = RabbitMQClient()
broadcaster = AlertBroadcaster()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")

# The dashboard is served from a separate origin in dev (Vite on :5173).
# Wide-open CORS is acceptable only because this whole cluster is local;
# restrict to the real dashboard origin before any shared deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _start_alert_stream() -> None:
    broadcaster.start(asyncio.get_running_loop())


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CostPoint(BaseModel):
    time: datetime
    cost_per_hour: float


class SummaryEntry(BaseModel):
    service: str
    cost_per_hour: float


class DashboardSummary(BaseModel):
    total_cost_per_hour: float
    top_services: list[SummaryEntry]


class AnomalyOut(BaseModel):
    id: str
    service: str
    score: float
    classification: str
    evidence: dict[str, Any]
    status: str
    created_at: datetime


class ActionOut(BaseModel):
    id: str
    anomaly_id: str | None
    action_type: str
    executed_at: datetime
    result: str
    rollback_ref: str | None


class PolicyIn(BaseModel):
    org_id: str = "default"
    rule_dsl: dict[str, Any] = Field(default_factory=dict)
    scope: str = "*"
    action: RemediationAction
    priority: int = 0


class PolicyOut(PolicyIn):
    id: str


class ActionDispatched(BaseModel):
    action_id: str
    status: str


def create_access_token(subject: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=config.TOKEN_EXPIRE_HOURS)
    payload = {"sub": subject, "role": role, "exp": expire}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict[str, str]:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    return {"username": payload["sub"], "role": payload["role"]}


def _publish_or_fail(action_id: str, revert_to: str, message: dict[str, Any]) -> None:
    """Publishes a remediation request, and repairs the audit row if it fails.

    The actions_log row has to exist before publishing, because the executor
    finalizes the outcome by updating that row. If the publish then fails,
    the row would sit at `dispatched` forever describing work that never
    happened — so it is moved to a terminal state instead, and the caller
    gets a 503 rather than a false success.
    """
    try:
        publish_mq.publish(ROUTING_KEY_REMEDIATION, message)
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(actions_log.update().where(actions_log.c.id == action_id).values(result=revert_to))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach the remediation queue; no action was taken.",
        ) from exc


def require_sre(user: dict[str, str] = Depends(get_current_user)) -> dict[str, str]:
    """Gate for every write/remediation endpoint (FR-14). A read-only
    finance user gets a 403 here, in the backend — the frontend hiding the
    button is UX, this is the actual control.
    """
    if user["role"] != "sre":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires the sre role",
        )
    return user


@app.post("/api/v1/auth/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    user = authenticate_user(form_data.username, form_data.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    return TokenResponse(access_token=create_access_token(user["username"], user["role"]))


@app.get("/api/v1/services/{service_id:path}/cost", response_model=list[CostPoint])
def get_service_cost(
    service_id: str,
    range_hours: int = 1,
    _user: dict[str, str] = Depends(get_current_user),
) -> list[CostPoint]:
    since = datetime.now(timezone.utc) - timedelta(hours=range_hours)
    stmt = (
        select(cost_events.c.time, cost_events.c.cost_per_hour)
        .where(cost_events.c.service == service_id, cost_events.c.time >= since)
        .order_by(cost_events.c.time)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [CostPoint(time=row.time, cost_per_hour=row.cost_per_hour) for row in rows]


@app.get("/api/v1/dashboard/summary", response_model=DashboardSummary)
def get_dashboard_summary(_user: dict[str, str] = Depends(get_current_user)) -> DashboardSummary:
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    stmt = (
        select(cost_events.c.service, cost_events.c.cost_per_hour)
        .where(cost_events.c.time >= since)
        .order_by(cost_events.c.service, desc(cost_events.c.time))
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()

    # rows are ordered latest-first within each service, so the first row
    # seen per service is its most recent cost_per_hour reading.
    latest_by_service: dict[str, float] = {}
    for row in rows:
        latest_by_service.setdefault(row.service, row.cost_per_hour)

    top_services = sorted(
        (SummaryEntry(service=s, cost_per_hour=c) for s, c in latest_by_service.items()),
        key=lambda entry: entry.cost_per_hour,
        reverse=True,
    )[:10]
    return DashboardSummary(total_cost_per_hour=sum(latest_by_service.values()), top_services=top_services)


@app.get("/api/v1/anomalies", response_model=list[AnomalyOut])
def list_anomalies(
    limit: int = 50,
    service: str | None = None,
    _user: dict[str, str] = Depends(get_current_user),
) -> list[AnomalyOut]:
    stmt = select(anomalies).order_by(desc(anomalies.c.created_at)).limit(limit)
    if service:
        stmt = stmt.where(anomalies.c.service == service)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [
        AnomalyOut(
            id=str(row.id),
            service=row.service,
            score=row.score,
            classification=row.classification,
            evidence=row.evidence,
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]


@app.get("/api/v1/actions", response_model=list[ActionOut])
def list_actions(limit: int = 50, _user: dict[str, str] = Depends(get_current_user)) -> list[ActionOut]:
    stmt = select(actions_log).order_by(desc(actions_log.c.executed_at)).limit(limit)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [
        ActionOut(
            id=str(row.id),
            anomaly_id=str(row.anomaly_id) if row.anomaly_id else None,
            action_type=row.action_type,
            executed_at=row.executed_at,
            result=row.result,
            rollback_ref=row.rollback_ref,
        )
        for row in rows
    ]


@app.get("/api/v1/policies", response_model=list[PolicyOut])
def list_policies(_user: dict[str, str] = Depends(get_current_user)) -> list[PolicyOut]:
    stmt = select(policies).order_by(desc(policies.c.priority))
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [
        PolicyOut(
            id=str(row.id),
            org_id=row.org_id,
            rule_dsl=row.rule_dsl,
            scope=row.scope,
            action=RemediationAction(row.action),
            priority=row.priority,
        )
        for row in rows
    ]


@app.post("/api/v1/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
def create_policy(policy: PolicyIn, _user: dict[str, str] = Depends(require_sre)) -> PolicyOut:
    policy_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            policies.insert().values(
                id=policy_id,
                org_id=policy.org_id,
                rule_dsl=policy.rule_dsl,
                scope=policy.scope,
                action=policy.action.value,
                priority=policy.priority,
            )
        )
    return PolicyOut(id=policy_id, **policy.model_dump())


@app.post("/api/v1/actions/{anomaly_id}/approve", response_model=ActionDispatched)
def approve_action(anomaly_id: str, _user: dict[str, str] = Depends(require_sre)) -> ActionDispatched:
    """Human-in-the-loop approval (FR-10) for an action decision-policy
    recorded as pending_approval. Dispatches it to the Remediation Executor.
    """
    stmt = (
        select(actions_log)
        .where(actions_log.c.anomaly_id == anomaly_id, actions_log.c.result == "pending_approval")
        .order_by(desc(actions_log.c.executed_at))
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No action pending approval for this anomaly")

    with engine.begin() as conn:
        conn.execute(actions_log.update().where(actions_log.c.id == row.id).values(result="dispatched"))

    anomaly_stmt = select(anomalies.c.service).where(anomalies.c.id == anomaly_id)
    with engine.connect() as conn:
        anomaly_row = conn.execute(anomaly_stmt).first()

    _publish_or_fail(
        action_id=str(row.id),
        revert_to="pending_approval",
        message={
            "action_id": str(row.id),
            "anomaly_id": anomaly_id,
            "service": anomaly_row.service,
            "action": row.action_type,
        },
    )
    return ActionDispatched(action_id=str(row.id), status="dispatched")


@app.post("/api/v1/actions/{anomaly_id}/rollback", response_model=ActionDispatched)
def rollback_action(anomaly_id: str, _user: dict[str, str] = Depends(require_sre)) -> ActionDispatched:
    """Reverts the most recent executed remediation for an anomaly (FR-11).

    Opens a NEW actions_log row for the rollback rather than mutating the
    original — actions_log is append-only audit evidence, so "we throttled
    it, then we undid it" must stay legible as two distinct events.
    """
    stmt = (
        select(actions_log)
        .where(actions_log.c.anomaly_id == anomaly_id, actions_log.c.result == "executed")
        .order_by(desc(actions_log.c.executed_at))
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No executed action to roll back for this anomaly")

    rollback_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            actions_log.insert().values(
                id=rollback_id,
                anomaly_id=anomaly_id,
                action_type="rollback",
                executed_at=datetime.now(timezone.utc),
                result="dispatched",
                rollback_ref=None,
            )
        )

    _publish_or_fail(
        action_id=rollback_id,
        revert_to="dispatch_failed",
        message={
            "action_id": rollback_id,
            "anomaly_id": anomaly_id,
            "service": "",
            "action": "rollback",
            "target_action_id": str(row.id),
        },
    )
    return ActionDispatched(action_id=rollback_id, status="dispatched")


@app.delete("/api/v1/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(policy_id: str, _user: dict[str, str] = Depends(require_sre)) -> None:
    with engine.begin() as conn:
        result = conn.execute(policies.delete().where(policies.c.id == policy_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Policy not found")


@app.websocket("/api/v1/stream/alerts")
async def stream_alerts(websocket: WebSocket, token: str = "") -> None:
    """Live anomaly feed for the dashboard (doc 5.3).

    The token arrives as a query parameter rather than a header because the
    browser WebSocket API cannot set custom headers on the handshake. It is
    still a normal JWT, verified before the connection is accepted.
    """
    try:
        jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except JWTError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    queue = broadcaster.subscribe()
    try:
        while True:
            alert = await queue.get()
            await websocket.send_json(alert)
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unsubscribe(queue)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
