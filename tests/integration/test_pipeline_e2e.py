"""End-to-end test of the detection pipeline across real service boundaries.

Publishes MetricEvents onto the live bus for a synthetic workload and
asserts that the deployed cost-intelligence and behaviour-analysis services
turn them into cost events and, on a spike, a flagged anomaly:

    metric.raw -> cost-intelligence -> cost.event -> behaviour-analysis -> anomaly

Nothing here reimplements the pipeline; it drives the running deployments
and reads the results out of TimescaleDB. That makes it the only test that
would catch a break *between* services — a routing key renamed on one side,
a schema field dropped, a consumer that stopped acking.

It writes as a synthetic `itest-` workload that matches no policy, so the
Decision Engine classifies it but never remediates. Slower than the rest of
the suite (tens of seconds) because it waits on real 10s-scale pipeline
behaviour.
"""

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from hypertrace_common.messaging import ROUTING_KEY_METRIC, ROUTING_KEY_TRAFFIC, RabbitMQClient
from hypertrace_common.schemas import MetricEvent, ResourceRef, TrafficSample
from hypertrace_common.tables import anomalies, cost_events

NAMESPACE = "itest-ns"
IDLE_CPU_CORES = 0.002
SPIKE_CPU_CORES = 1.0
BASELINE_SAMPLES = 10  # comfortably past MIN_SAMPLES_FOR_DETECTION, plus dwell
BASELINE_RPS = 5.0     # the business signal the detector compares against


def _publish_sample(mq: RabbitMQClient, workload: str, cpu_cores: float, jitter: int = 0) -> None:
    """Emits one MetricEvent shaped exactly as the collector emits them.

    A little jitter on memory matters: with a perfectly constant metric the
    baseline's stddev is zero and z_score returns 0 by design, so nothing
    would ever flag (see KNOWN-LIMITATIONS §6).
    """
    mq.publish(
        ROUTING_KEY_METRIC,
        MetricEvent(
            timestamp=datetime.now(timezone.utc),
            resource=ResourceRef(
                cluster="itest",
                namespace=NAMESPACE,
                node="itest-node",
                pod=f"{workload}-pod",
                service=workload,
            ),
            cpu_usage_cores=cpu_cores,
            memory_working_set_bytes=32 * 1024 * 1024 + jitter,
        ).model_dump(),
    )


def _publish_traffic(mq: RabbitMQClient, service_id: str, rps: float, jitter: float = 0.0) -> None:
    """Emits the business signal for a synthetic workload.

    The detector refuses to flag a service it has no traffic reading for —
    an absent signal is not zero traffic — so an end-to-end test has to
    supply this the way the traffic adapter would.
    """
    mq.publish(
        ROUTING_KEY_TRAFFIC,
        TrafficSample(
            timestamp=datetime.now(timezone.utc), service=service_id, requests_per_second=rps + jitter
        ).model_dump(),
    )


def _wait_for(predicate, timeout: float, interval: float = 1.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")


def _count_cost_events(engine, service_id: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(cost_events).where(cost_events.c.service == service_id)
        ).scalar_one()


def _anomalies_for(engine, service_id: str) -> list:
    with engine.connect() as conn:
        return conn.execute(
            select(anomalies.c.score, anomalies.c.classification, anomalies.c.evidence)
            .where(anomalies.c.service == service_id)
            .order_by(anomalies.c.created_at.desc())
        ).all()


@pytest.fixture
def workload(unique_service, db_cleanup):
    """A synthetic workload id plus the service id it becomes downstream.

    cost-intelligence keys cost on `namespace/workload`, so the id the rest
    of the pipeline sees is not the raw name published here.
    """
    service_id = f"{NAMESPACE}/{unique_service}"
    db_cleanup(service_id)
    return unique_service, service_id


class TestDetectionPipeline:
    def test_metrics_become_cost_events(self, mq_settings, db_engine, workload):
        """First hop: the deployed cost-intelligence service must price a
        published MetricEvent and persist it.
        """
        name, service_id = workload
        mq = RabbitMQClient(mq_settings)
        for i in range(3):
            _publish_sample(mq, name, IDLE_CPU_CORES, jitter=i * 1024)

        _wait_for(
            lambda: _count_cost_events(db_engine, service_id) >= 3,
            timeout=45,
            what=f"cost_events rows for {service_id}",
        )

        with db_engine.connect() as conn:
            row = conn.execute(
                select(cost_events.c.cost_per_hour, cost_events.c.resource_type)
                .where(cost_events.c.service == service_id)
                .limit(1)
            ).one()
        assert row.cost_per_hour > 0, "a workload burning CPU must cost something"
        assert row.resource_type == "compute"

    def test_cost_scales_with_cpu(self, mq_settings, db_engine, workload):
        """The pricing model is what makes cost a usable signal — a workload
        using ~500x the CPU must not produce a similar figure.
        """
        name, service_id = workload
        mq = RabbitMQClient(mq_settings)

        _publish_sample(mq, name, IDLE_CPU_CORES)
        _wait_for(lambda: _count_cost_events(db_engine, service_id) >= 1, timeout=45, what="the idle cost event")
        with db_engine.connect() as conn:
            idle = conn.execute(
                select(cost_events.c.cost_per_hour).where(cost_events.c.service == service_id)
            ).scalars().all()[0]

        _publish_sample(mq, name, SPIKE_CPU_CORES)
        _wait_for(lambda: _count_cost_events(db_engine, service_id) >= 2, timeout=45, what="the spike cost event")
        with db_engine.connect() as conn:
            costs = conn.execute(
                select(cost_events.c.cost_per_hour)
                .where(cost_events.c.service == service_id)
                .order_by(cost_events.c.time.desc())
            ).scalars().all()

        assert max(costs) > idle * 10, f"spike cost {max(costs)} should dwarf idle cost {idle}"

    @pytest.mark.slow
    def test_a_cost_spike_is_flagged_as_an_anomaly(self, mq_settings, db_engine, workload):
        """The full loop: establish a baseline, then spike, and require the
        deployed behaviour-analysis service to flag it.

        This is the test that would catch a break anywhere between the bus
        and the anomaly table.
        """
        name, service_id = workload
        mq = RabbitMQClient(mq_settings)

        for i in range(BASELINE_SAMPLES):
            _publish_sample(mq, name, IDLE_CPU_CORES, jitter=i * 4096)
            _publish_traffic(mq, service_id, BASELINE_RPS, jitter=(i % 3) * 0.1)
            time.sleep(0.4)  # let each sample land in order

        _wait_for(
            lambda: _count_cost_events(db_engine, service_id) >= BASELINE_SAMPLES,
            timeout=60,
            what="the baseline to be built",
        )
        assert not _anomalies_for(db_engine, service_id), "a steady workload must not flag — that would be a false positive"

        # Traffic stays flat while cost spikes — the decoupling condition.
        for _ in range(5):
            _publish_sample(mq, name, SPIKE_CPU_CORES)
            _publish_traffic(mq, service_id, BASELINE_RPS)
            time.sleep(0.4)

        found = _wait_for(
            lambda: _anomalies_for(db_engine, service_id),
            timeout=60,
            what=f"an anomaly for {service_id}",
        )
        top = found[0]
        assert abs(top.score) > 3, f"z-score {top.score} should exceed the detector's threshold"
        assert top.evidence.get("metric") == "cost_per_hour"
        # decision-policy classifies asynchronously; either state is valid the
        # instant we read it, but it must be a known one.
        assert top.classification in {
            "unclassified",
            "misconfiguration_or_waste",
            "likely_bug_from_deployment",
            "suspected_abuse",
        }

    @pytest.mark.slow
    def test_no_policy_means_no_remediation(self, mq_settings, db_engine, workload):
        """A flagged anomaly on a workload no policy covers must stay
        alert-only. The safe default matters more than the alerting itself:
        acting without a matching policy would be the worst failure this
        system could have.
        """
        from hypertrace_common.tables import actions_log

        name, service_id = workload
        mq = RabbitMQClient(mq_settings)

        for i in range(BASELINE_SAMPLES):
            _publish_sample(mq, name, IDLE_CPU_CORES, jitter=i * 4096)
            _publish_traffic(mq, service_id, BASELINE_RPS, jitter=(i % 3) * 0.1)
            time.sleep(0.4)
        _wait_for(
            lambda: _count_cost_events(db_engine, service_id) >= BASELINE_SAMPLES,
            timeout=60,
            what="the baseline to be built",
        )
        for _ in range(5):
            _publish_sample(mq, name, SPIKE_CPU_CORES)
            _publish_traffic(mq, service_id, BASELINE_RPS)
            time.sleep(0.4)
        _wait_for(lambda: _anomalies_for(db_engine, service_id), timeout=60, what="an anomaly")

        time.sleep(5)  # give decision-policy room to act, if it were going to
        with db_engine.connect() as conn:
            action_count = conn.execute(
                select(func.count())
                .select_from(actions_log)
                .join(anomalies, anomalies.c.id == actions_log.c.anomaly_id)
                .where(anomalies.c.service == service_id)
            ).scalar_one()
        assert action_count == 0, "no policy matches this workload, so nothing should have been actioned"
