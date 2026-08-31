"""Remediation Executor entrypoint (doc Section 3 Phase 4, FR-8/FR-9/FR-11).

The only component in the system holding Kubernetes *write* credentials,
and deliberately the narrowest one: its ServiceAccount can patch
Deployments and HorizontalPodAutoscalers and nothing else (see
infra/k8s/01-rbac.yaml) — it cannot delete pods, read Secrets, or touch
any other resource kind (NFR-4).

Consumes remediation.requested messages and executes them, then writes the
outcome plus a rollback reference back to the append-only actions_log
(FR-9). Three independent safety gates apply before anything is patched:

  1. Protected-namespace floor — re-checked here even though
     decision-policy already checked it (doc 11.3, defense in depth).
  2. Rate limit — at most MAX_ACTIONS_PER_WINDOW executions per rolling
     window, so a bug upstream can't cascade.
  3. Idempotency — k8s_actions returns "no_op" rather than re-patching a
     workload that is already in the requested state (doc 5.4).

Rollbacks (FR-11) arrive through this same queue rather than being applied
by the API service directly, so cluster write access stays confined to
this one component.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from kubernetes import client, config as k8s_config
from sqlalchemy import func, select

from hypertrace_common.db import make_engine
from hypertrace_common.messaging import ROUTING_KEY_REMEDIATION, RabbitMQClient
from hypertrace_common.schemas import RemediationAction
from hypertrace_common.tables import actions_log

from . import config
from .k8s_actions import freeze_scaling, rollback, throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("remediation-executor")

_EXECUTED_RESULTS = ("executed", "rolled_back")


def _is_protected(service: str) -> bool:
    return any(service.startswith(prefix) for prefix in config.PROTECTED_NAMESPACE_PREFIXES)


def _rate_limit_exceeded(engine, service: str) -> str | None:
    """Returns the name of the ceiling that was hit, or None to proceed.

    Two independent limits (§17.2). The global one stops a bug upstream
    cascading across the cluster; the per-service one stops a single noisy
    workload consuming the whole global budget and starving remediation
    everywhere else.
    """
    now = datetime.now(timezone.utc)

    global_since = now - timedelta(minutes=config.RATE_LIMIT_WINDOW_MINUTES)
    service_since = now - timedelta(minutes=config.SERVICE_RATE_LIMIT_WINDOW_MINUTES)

    global_stmt = select(func.count()).select_from(actions_log).where(
        actions_log.c.executed_at >= global_since,
        actions_log.c.result.in_(_EXECUTED_RESULTS),
    )
    # Matches on the recorded target rather than joining anomalies, so the
    # limit still applies to actions whose anomaly row has been trimmed.
    service_stmt = select(func.count()).select_from(actions_log).where(
        actions_log.c.executed_at >= service_since,
        actions_log.c.result.in_(_EXECUTED_RESULTS),
        actions_log.c.target["service"].astext == service,
    )

    with engine.connect() as conn:
        if conn.execute(global_stmt).scalar_one() >= config.MAX_ACTIONS_PER_WINDOW:
            return "global"
        if conn.execute(service_stmt).scalar_one() >= config.MAX_ACTIONS_PER_SERVICE:
            return "per_service"
    return None


def _record(
    engine,
    *,
    anomaly_id: str | None,
    parent_action_id: str | None,
    action_type: str,
    result: str,
    service: str,
    mode: str = "autonomous",
    prior_state: dict | None = None,
    applied_state: dict | None = None,
    rollback_ref: str | None = None,
    set_rollback_deadline: bool = False,
) -> str:
    """Appends one row to the audit ledger.

    Never updates: `actions_log` is append-only and the database rejects
    UPDATE outright (§21.4). The outcome of a decision is a new row
    pointing back at that decision through parent_action_id, so the record
    of what was requested survives alongside what actually happened.
    """
    now = datetime.now(timezone.utc)
    action_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            actions_log.insert().values(
                id=action_id,
                anomaly_id=anomaly_id,
                parent_action_id=parent_action_id,
                rollback_ref=rollback_ref,
                action_type=action_type,
                mode=mode,
                executed_at=now,
                result=result,
                target={"service": service},
                prior_state=prior_state,
                applied_state=applied_state,
                rollback_deadline=(
                    now + timedelta(minutes=config.ROLLBACK_WINDOW_MINUTES) if set_rollback_deadline else None
                ),
            )
        )
    return action_id


def _load_action(engine, action_id: str):
    stmt = select(actions_log).where(actions_log.c.id == action_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first()


def main() -> None:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    autoscaling_v2 = client.AutoscalingV2Api()
    engine = make_engine()
    consume_mq = RabbitMQClient()

    def handle(message: dict[str, Any]) -> None:
        decision_id = message["action_id"]  # the decision row this outcome answers
        anomaly_id = message.get("anomaly_id")
        service = message["service"]
        action = message["action"]
        mode = message.get("mode", "autonomous")

        def record(result: str, **kwargs) -> str:
            return _record(
                engine,
                anomaly_id=anomaly_id,
                parent_action_id=decision_id,
                action_type=action,
                result=result,
                service=service,
                mode=mode,
                **kwargs,
            )

        if action == "rollback":
            target = _load_action(engine, message["target_action_id"])
            if target is None or not target.prior_state:
                record("rollback_failed_no_reference")
                logger.warning("decision=%s: nothing to roll back", decision_id)
                return

            # Past the deadline the recorded prior state is no longer safe to
            # restore: the workload may legitimately have changed since, and
            # reapplying a stale snapshot would revert someone else's work.
            deadline = target.rollback_deadline
            if deadline is not None and datetime.now(timezone.utc) > deadline:
                record("rollback_window_expired", rollback_ref=str(target.id))
                logger.warning("decision=%s: rollback window expired at %s", decision_id, deadline)
                return

            outcome = rollback(apps_v1, autoscaling_v2, json.dumps(target.prior_state))
            record(outcome["status"], rollback_ref=str(target.id))
            logger.info("decision=%s rollback: %s", decision_id, outcome["reason"])
            return

        if _is_protected(service):
            record("blocked_by_protected_floor")
            logger.warning("decision=%s service=%s is protected — refusing to act", decision_id, service)
            return

        limit = _rate_limit_exceeded(engine, service)
        if limit is not None:
            record(f"blocked_by_{limit}_rate_limit")
            logger.warning("decision=%s blocked by the %s rate limit", decision_id, limit)
            return

        if action == RemediationAction.THROTTLE.value:
            outcome = throttle(apps_v1, core_v1, service)
        elif action == RemediationAction.FREEZE_SCALING.value:
            outcome = freeze_scaling(autoscaling_v2, apps_v1, core_v1, service)
        else:
            # quarantine/terminate are intentionally not implemented — see
            # the module docstring and KNOWN-LIMITATIONS.md §9.
            record(f"unsupported_action_{action}")
            logger.error("decision=%s: unsupported action %r", decision_id, action)
            return

        executed = outcome["status"] == "executed"
        record(
            outcome["status"],
            prior_state=json.loads(outcome["rollback_ref"]) if outcome["rollback_ref"] else None,
            applied_state={"reason": outcome["reason"]},
            set_rollback_deadline=executed,
        )
        logger.info("decision=%s service=%s %s: %s", decision_id, service, outcome["status"], outcome["reason"])

    logger.info(
        "remediation-executor starting: global=%d/%dmin per-service=%d/%dmin "
        "rollback-window=%dmin protected=%s",
        config.MAX_ACTIONS_PER_WINDOW,
        config.RATE_LIMIT_WINDOW_MINUTES,
        config.MAX_ACTIONS_PER_SERVICE,
        config.SERVICE_RATE_LIMIT_WINDOW_MINUTES,
        config.ROLLBACK_WINDOW_MINUTES,
        config.PROTECTED_NAMESPACE_PREFIXES,
    )
    consume_mq.consume(
        queue="remediation-executor.actions",
        routing_keys=[ROUTING_KEY_REMEDIATION],
        on_message=handle,
    )


if __name__ == "__main__":
    main()
