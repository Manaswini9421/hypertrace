"""Decision & Policy Engine entrypoint (doc Section 3 Phase 4, FR-7/FR-10).

Consumes anomaly.flagged events (from Phase 3's Behaviour Analysis Engine)
and event.lifecycle events (from the Phase 1 collector) to do the joint
classification from doc 14.3, then evaluates org policies (doc 14.4) to
decide whether — and how — to act.

Honest scope note: doc 14.3's full pseudocode branches on cpu_z, traffic_z,
cost_z, egress/process security signatures, and recent-deployment timing.
This prototype has cost-based anomaly scores and security signals, but no
request-count/traffic telemetry, so the classification below is a genuine
subset of that pseudocode. In particular it does NOT attempt to distinguish
"legitimate_traffic_growth" from a bug, because that requires traffic data
this prototype doesn't collect — see docs/report.html Section 9.2, "what
you should NOT claim."

Both correlations are real: a cost anomaly landing within
SECURITY_SIGNAL_WINDOW_MINUTES of a security signal for the same service
becomes `suspected_abuse`, and one landing shortly after a deployment
becomes `likely_bug_from_deployment`. The security signals themselves
currently come from the synthetic emitter in
services/security-signal-adapter rather than a real Falco deployment —
the ingestion contract is production-shaped, the producer is not.

Recent-deployment tracking is kept in an in-memory dict, which is only
correct with a single replica (this Deployment is intentionally
replicas: 1) — a second replica would each have a partial view of recent
events. Moving it to shared storage is a Phase 7 hardening item if this
ever needs to scale out.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from hypertrace_common.db import make_engine
from hypertrace_common.messaging import (
    ROUTING_KEY_ANOMALY,
    ROUTING_KEY_LIFECYCLE,
    ROUTING_KEY_REMEDIATION,
    ROUTING_KEY_SECURITY,
    RabbitMQClient,
)
from hypertrace_common.schemas import AnomalyClassification, RemediationAction
from hypertrace_common.tables import actions_log, anomalies, policies

from . import config
from .policy import is_protected, policy_matches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("decision-policy")

_DEPLOYMENT_LIKE_EVENTS = {"deployment_scaled", "pod_restarted", "pod_created", "hpa_scaled"}

_recent_events: dict[str, datetime] = {}
_recent_events_lock = threading.Lock()

_recent_security: dict[str, tuple[datetime, str]] = {}
_recent_security_lock = threading.Lock()


def _handle_lifecycle(message: dict[str, Any]) -> None:
    if message["event_type"] not in _DEPLOYMENT_LIKE_EVENTS:
        return
    resource = message["resource"]
    pod = resource.get("pod")
    service = f"{resource.get('namespace')}/{pod}" if pod else resource.get("namespace")
    if not service:
        return
    with _recent_events_lock:
        _recent_events[service] = datetime.now(timezone.utc)


def _recent_deployment(service: str) -> bool:
    with _recent_events_lock:
        last_event = _recent_events.get(service)
    if last_event is None:
        return False
    return datetime.now(timezone.utc) - last_event <= timedelta(minutes=config.RECENT_DEPLOYMENT_WINDOW_MINUTES)


def _handle_security_signal(message: dict[str, Any]) -> None:
    with _recent_security_lock:
        _recent_security[message["service"]] = (datetime.now(timezone.utc), message["rule"])


def _recent_security_signal(service: str) -> str | None:
    """Returns the corroborating rule name if this service tripped a
    security rule recently, else None.
    """
    with _recent_security_lock:
        entry = _recent_security.get(service)
    if entry is None:
        return None
    seen_at, rule = entry
    if datetime.now(timezone.utc) - seen_at > timedelta(minutes=config.SECURITY_SIGNAL_WINDOW_MINUTES):
        return None
    return rule


def _classify(service: str) -> tuple[AnomalyClassification, dict[str, Any]]:
    """Joint classification (doc 14.3). Returns the classification plus the
    corroborating evidence for it, so the audit trail and the dashboard can
    both show *why* a verdict was reached rather than just the verdict.

    Security corroboration outranks deployment correlation: a workload that
    is both freshly deployed and tripping a security rule is the more
    dangerous reading, and doc 11.1 argues for erring toward the
    higher-confidence abuse signal when both are present.
    """
    rule = _recent_security_signal(service)
    if rule is not None:
        return AnomalyClassification.SUSPECTED_ABUSE, {"security_rule": rule}
    if _recent_deployment(service):
        return AnomalyClassification.LIKELY_BUG_FROM_DEPLOYMENT, {"recent_deployment": True}
    return AnomalyClassification.MISCONFIGURATION_OR_WASTE, {}


def _find_matching_policy(
    engine, classification: str, service: str, cost_per_hour: float
) -> tuple[dict[str, Any], str] | None:
    stmt = select(policies.c.rule_dsl, policies.c.action).order_by(policies.c.priority.desc())
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    for row in rows:
        if policy_matches(row.rule_dsl, classification, service, cost_per_hour):
            return dict(row.rule_dsl), row.action
    return None


def _record_action(engine, anomaly_id: str, action: RemediationAction, result: str) -> str:
    action_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            actions_log.insert().values(
                id=action_id,
                anomaly_id=anomaly_id,
                action_type=action.value,
                executed_at=datetime.now(timezone.utc),
                result=result,
                rollback_ref=None,
            )
        )
    return action_id


def _handle_anomaly(engine, publish_mq: RabbitMQClient, message: dict[str, Any]) -> None:
    anomaly_id = message["id"]
    service = message["service"]
    cost_per_hour = message["evidence"].get("value", 0.0)

    classification, reason = _classify(service)
    with engine.begin() as conn:
        # Merge the classification reason into the existing evidence blob so
        # the record shows both the detection evidence (z-score, metric) and
        # why it was classified the way it was (FR-9).
        merged_evidence = {**message.get("evidence", {}), "classification_reason": reason}
        conn.execute(
            anomalies.update()
            .where(anomalies.c.id == anomaly_id)
            .values(classification=classification.value, evidence=merged_evidence)
        )

    matched = _find_matching_policy(engine, classification.value, service, cost_per_hour)
    if matched is None:
        logger.info(
            "anomaly=%s service=%s classification=%s: no policy matched, alert only",
            anomaly_id,
            service,
            classification.value,
        )
        return

    rule_dsl, action = matched
    action_enum = RemediationAction(action)

    if is_protected(service, config.PROTECTED_NAMESPACE_PREFIXES):
        logger.warning("anomaly=%s service=%s matched a policy but is protected — blocked", anomaly_id, service)
        _record_action(engine, anomaly_id, action_enum, result="blocked_by_protected_floor")
        return

    if action_enum == RemediationAction.ALERT_ONLY:
        _record_action(engine, anomaly_id, action_enum, result="alert_only_no_action_taken")
        return

    if rule_dsl.get("requires_approval"):
        _record_action(engine, anomaly_id, action_enum, result="pending_approval")
        logger.info("anomaly=%s action=%s recorded as pending_approval", anomaly_id, action)
        return

    action_id = _record_action(engine, anomaly_id, action_enum, result="dispatched")
    publish_mq.publish(
        ROUTING_KEY_REMEDIATION,
        {"action_id": action_id, "anomaly_id": anomaly_id, "service": service, "action": action},
    )
    logger.info("anomaly=%s action=%s dispatched for service=%s", anomaly_id, action, service)


def main() -> None:
    engine = make_engine()
    publish_mq = RabbitMQClient()
    lifecycle_mq = RabbitMQClient()
    security_mq = RabbitMQClient()
    anomaly_mq = RabbitMQClient()

    lifecycle_thread = threading.Thread(
        target=lambda: lifecycle_mq.consume(
            queue="decision-policy.lifecycle",
            routing_keys=[ROUTING_KEY_LIFECYCLE],
            on_message=_handle_lifecycle,
        ),
        daemon=True,
        name="lifecycle-consumer",
    )
    lifecycle_thread.start()

    security_thread = threading.Thread(
        target=lambda: security_mq.consume(
            queue="decision-policy.security",
            routing_keys=[ROUTING_KEY_SECURITY],
            on_message=_handle_security_signal,
        ),
        daemon=True,
        name="security-consumer",
    )
    security_thread.start()

    logger.info("decision-policy starting: protected=%s", config.PROTECTED_NAMESPACE_PREFIXES)
    anomaly_mq.consume(
        queue="decision-policy.anomalies",
        routing_keys=[ROUTING_KEY_ANOMALY],
        on_message=lambda msg: _handle_anomaly(engine, publish_mq, msg),
    )


if __name__ == "__main__":
    main()
