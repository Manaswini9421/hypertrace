"""Decision & Policy Engine entrypoint (doc Section 3 Phase 4, FR-7/FR-10).

Consumes anomaly.flagged events (from Phase 3's Behaviour Analysis Engine)
and event.lifecycle events (from the Phase 1 collector) to do the joint
classification from doc 14.3, then evaluates org policies (doc 14.4) to
decide whether — and how — to act.

The detector has already applied the decoupling test and scored its own
confidence; this service decides *cause* and *authority*. Where the detector
concluded that traffic explained the movement, that verdict stands — cause
is settled and nothing is authorised.

Authority is a function of confidence and only of confidence (§24.3):
below 0.60 an anomaly may only alert, below 0.85 it may only be recommended
for human approval. Policy can narrow that further but never widen it, so a
rule demanding autonomous action on a weak signal still gets approval or
nothing.

Both corroborations are real: a cost anomaly landing within
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
from .authority import authority_for
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


def _classify(service: str, detector_verdict: str | None) -> tuple[AnomalyClassification, dict[str, Any]]:
    """Joint classification (§24.4). Returns the classification plus the
    corroborating evidence for it, so the audit trail and the dashboard can
    both show *why* a verdict was reached rather than just the verdict.

    A `legitimate_traffic_growth` verdict from the detector is final: it
    means traffic rose alongside cost, which explains the movement outright.
    Re-deriving a cause here would discard the one signal that settles it.

    Otherwise, security corroboration outranks deployment correlation — a
    workload that is both freshly deployed and tripping a security rule is
    the more dangerous reading.
    """
    if detector_verdict == AnomalyClassification.LEGITIMATE_TRAFFIC_GROWTH.value:
        return AnomalyClassification.LEGITIMATE_TRAFFIC_GROWTH, {"traffic_explains": True}

    rule = _recent_security_signal(service)
    if rule is not None:
        return AnomalyClassification.SUSPECTED_ABUSE, {"security_rule": rule}
    if _recent_deployment(service):
        return AnomalyClassification.LIKELY_BUG_FROM_DEPLOYMENT, {"recent_deployment": True}
    return AnomalyClassification.MISCONFIGURATION_OR_WASTE, {}


def _find_matching_policy(
    engine, classification: str, service: str, cost_per_hour: float, confidence: float
) -> tuple[dict[str, Any], str] | None:
    # Lower priority number wins (dossier §25.1: "priority: 100  # lower
    # number wins"). This was previously ordered DESC, which evaluated any
    # spec-authored policy set in exactly the wrong order — a low-numbered
    # "never touch payments" rule would have lost to a high-numbered
    # catch-all instead of beating it.
    stmt = select(policies.c.rule_dsl, policies.c.action).order_by(policies.c.priority.asc())
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    for row in rows:
        if policy_matches(row.rule_dsl, classification, service, cost_per_hour, confidence):
            return dict(row.rule_dsl), row.action
    return None


def _record_action(
    engine, anomaly_id: str, action: RemediationAction, result: str, service: str, mode: str = "autonomous"
) -> str:
    """Appends the *decision* to the ledger.

    This row records what was decided, not what happened — the executor
    appends a separate outcome row pointing back at this one. `actions_log`
    is append-only and the database rejects UPDATE (§21.4), so the two must
    never be collapsed into one mutated row.
    """
    action_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            actions_log.insert().values(
                id=action_id,
                anomaly_id=anomaly_id,
                action_type=action.value,
                mode=mode,
                executed_at=datetime.now(timezone.utc),
                result=result,
                target={"service": service},
            )
        )
    return action_id


def _handle_anomaly(engine, publish_mq: RabbitMQClient, message: dict[str, Any]) -> None:
    anomaly_id = message["id"]
    service = message["service"]
    cost_per_hour = message["evidence"].get("value", 0.0)
    # Absent confidence means an anomaly from an older detector build. Treat
    # it as advisory rather than assuming full confidence — the safe
    # direction when the signal that gates authority is missing.
    score_confidence = float(message.get("confidence") or message["evidence"].get("confidence") or 0.0)

    classification, reason = _classify(service, message.get("classification"))
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

    # A classification that explains the movement authorises nothing, so
    # there is no policy to look for (§24.4).
    if classification == AnomalyClassification.LEGITIMATE_TRAFFIC_GROWTH:
        logger.info("anomaly=%s service=%s: traffic explains the spike, no action", anomaly_id, service)
        return

    matched = _find_matching_policy(engine, classification.value, service, cost_per_hour, score_confidence)
    if matched is None:
        logger.info(
            "anomaly=%s service=%s classification=%s confidence=%.2f: no policy matched, alert only",
            anomaly_id,
            service,
            classification.value,
            score_confidence,
        )
        return

    rule_dsl, action = matched
    action_enum = RemediationAction(action)

    if is_protected(service, config.PROTECTED_NAMESPACE_PREFIXES):
        logger.warning("anomaly=%s service=%s matched a policy but is protected — blocked", anomaly_id, service)
        _record_action(engine, anomaly_id, action_enum, "blocked_by_protected_floor", service)
        return

    if action_enum == RemediationAction.ALERT_ONLY:
        _record_action(engine, anomaly_id, action_enum, "alert_only_no_action_taken", service)
        return

    # Authority is a function of confidence and only of confidence (§24.3).
    # Policy may narrow what this permits but never widen it: a rule asking
    # for autonomous action on a low-confidence anomaly still gets approval
    # or nothing.
    authority = authority_for(score_confidence)

    if authority == "alert_only":
        _record_action(engine, anomaly_id, action_enum, "alert_only_low_confidence", service)
        logger.info(
            "anomaly=%s action=%s withheld: confidence %.2f is below the approval threshold",
            anomaly_id,
            action,
            score_confidence,
        )
        return

    if authority == "approval" or rule_dsl.get("requires_approval"):
        _record_action(engine, anomaly_id, action_enum, "pending_approval", service, mode="approved")
        logger.info(
            "anomaly=%s action=%s needs approval (confidence=%.2f)", anomaly_id, action, score_confidence
        )
        return

    action_id = _record_action(engine, anomaly_id, action_enum, "dispatched", service)
    publish_mq.publish(
        ROUTING_KEY_REMEDIATION,
        {
            "action_id": action_id,
            "anomaly_id": anomaly_id,
            "service": service,
            "action": action,
            "mode": "autonomous",
        },
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
