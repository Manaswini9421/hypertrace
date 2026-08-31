"""Cost Intelligence Engine entrypoint (doc Section 3 Phase 2, FR-3).

Consumes MetricEvent messages off RabbitMQ, multiplies live resource usage
by the pricing model to get a $/hour figure, publishes a CostEvent for any
downstream consumer (the Phase 3 Behaviour Analysis Engine will be the
next one), and persists it to the cost_events hypertable for the API-BFF
to query.

Cost is only computed for pod-level MetricEvents (pod is not null) — the
collector also emits one node-level aggregate MetricEvent per node per
cycle, which isn't billable to any one workload and is skipped here.
"service" is namespace/pod for now (no Deployment-name resolution yet);
that's a deliberate Phase 2 scope cut, not an oversight.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert

from hypertrace_common.db import make_engine
from hypertrace_common.messaging import ROUTING_KEY_COST, ROUTING_KEY_METRIC, RabbitMQClient
from hypertrace_common.schemas import CostEvent
from hypertrace_common.tables import cost_events

from . import config
from .pricing import load_pricing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cost-intelligence")


def _service_id(resource: dict[str, Any]) -> str | None:
    """Stable identity for a billable workload: `namespace/workload`.

    Uses the owning workload name the collector resolved (Deployment,
    DaemonSet, ...) rather than the pod name, so a workload's cost history
    and behavioural baseline survive restarts — including the restarts
    HyperTrace itself causes when it throttles a Deployment. See
    services/collector/app/workload_resolver.py.

    Falls back to the pod name only if the collector could not resolve an
    owner (bare pods), and returns None for node-level aggregate events,
    which aren't billable to any single workload.
    """
    if not resource.get("pod"):
        return None
    workload = resource.get("service") or resource["pod"]
    return f"{resource.get('namespace')}/{workload}"


def main() -> None:
    pricing = load_pricing(config.PRICING_CONFIG_PATH)
    engine = make_engine()
    consume_mq = RabbitMQClient()
    publish_mq = RabbitMQClient()

    def handle(message: dict[str, Any]) -> None:
        service = _service_id(message["resource"])
        if service is None:
            return

        cost_per_hour = pricing.cost_per_hour(
            cpu_cores=message["cpu_usage_cores"],
            memory_bytes=message["memory_working_set_bytes"],
        )
        now = datetime.now(timezone.utc)

        with engine.begin() as conn:
            conn.execute(
                insert(cost_events).values(
                    time=now,
                    service=service,
                    resource_type="compute",
                    unit_rate=pricing.cpu_core_hour,
                    cost_per_hour=cost_per_hour,
                )
            )

        publish_mq.publish(
            ROUTING_KEY_COST,
            CostEvent(
                timestamp=now,
                service=service,
                resource=message["resource"],
                resource_type="compute",
                unit_rate=pricing.cpu_core_hour,
                cost_per_hour=cost_per_hour,
            ).model_dump(),
        )
        logger.info("service=%s cost_per_hour=$%.6f", service, cost_per_hour)

    logger.info(
        "cost-intelligence starting: cpu=$%s/core-hr mem=$%s/GB-hr",
        pricing.cpu_core_hour,
        pricing.memory_gb_hour,
    )
    consume_mq.consume(
        queue="cost-intelligence.metrics",
        routing_keys=[ROUTING_KEY_METRIC],
        on_message=handle,
    )


if __name__ == "__main__":
    main()
