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

import logging
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


def _rate_limit_exceeded(engine) -> bool:
    since = datetime.now(timezone.utc) - timedelta(minutes=config.RATE_LIMIT_WINDOW_MINUTES)
    stmt = select(func.count()).select_from(actions_log).where(
        actions_log.c.executed_at >= since,
        actions_log.c.result.in_(_EXECUTED_RESULTS),
    )
    with engine.connect() as conn:
        count = conn.execute(stmt).scalar_one()
    return count >= config.MAX_ACTIONS_PER_WINDOW


def _finalize(engine, action_id: str, result: str, rollback_ref: str | None = None) -> None:
    """Records the outcome against the actions_log row decision-policy
    already opened for this action. The row's identity and action_type are
    never rewritten — only the outcome fields — so the audit trail keeps
    showing what was requested alongside what actually happened.
    """
    with engine.begin() as conn:
        conn.execute(
            actions_log.update()
            .where(actions_log.c.id == action_id)
            .values(result=result, rollback_ref=rollback_ref, executed_at=datetime.now(timezone.utc))
        )


def _load_rollback_ref(engine, action_id: str) -> str | None:
    stmt = select(actions_log.c.rollback_ref).where(actions_log.c.id == action_id)
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row.rollback_ref if row else None


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
        action_id = message["action_id"]
        service = message["service"]
        action = message["action"]

        if action == "rollback":
            target_action_id = message["target_action_id"]
            ref = _load_rollback_ref(engine, target_action_id)
            if not ref:
                _finalize(engine, action_id, result="rollback_failed_no_reference")
                logger.warning("action=%s: nothing to roll back for target=%s", action_id, target_action_id)
                return
            outcome = rollback(apps_v1, autoscaling_v2, ref)
            _finalize(engine, action_id, result=outcome["status"])
            logger.info("action=%s rollback: %s", action_id, outcome["reason"])
            return

        if _is_protected(service):
            _finalize(engine, action_id, result="blocked_by_protected_floor")
            logger.warning("action=%s service=%s is protected — refusing to act", action_id, service)
            return

        if _rate_limit_exceeded(engine):
            _finalize(engine, action_id, result="blocked_by_rate_limit")
            logger.warning(
                "action=%s blocked: >=%d actions already executed in the last %d minutes",
                action_id,
                config.MAX_ACTIONS_PER_WINDOW,
                config.RATE_LIMIT_WINDOW_MINUTES,
            )
            return

        if action == RemediationAction.THROTTLE.value:
            outcome = throttle(apps_v1, core_v1, service)
        elif action == RemediationAction.FREEZE_SCALING.value:
            outcome = freeze_scaling(autoscaling_v2, apps_v1, core_v1, service)
        else:
            # quarantine/terminate are intentionally not implemented — see
            # the module docstring and doc 11.7.
            _finalize(engine, action_id, result=f"unsupported_action_{action}")
            logger.error("action=%s: unsupported action %r", action_id, action)
            return

        _finalize(engine, action_id, result=outcome["status"], rollback_ref=outcome["rollback_ref"])
        logger.info("action=%s service=%s %s: %s", action_id, service, outcome["status"], outcome["reason"])

    logger.info(
        "remediation-executor starting: rate_limit=%d/%dmin protected=%s",
        config.MAX_ACTIONS_PER_WINDOW,
        config.RATE_LIMIT_WINDOW_MINUTES,
        config.PROTECTED_NAMESPACE_PREFIXES,
    )
    consume_mq.consume(
        queue="remediation-executor.actions",
        routing_keys=[ROUTING_KEY_REMEDIATION],
        on_message=handle,
    )


if __name__ == "__main__":
    main()
