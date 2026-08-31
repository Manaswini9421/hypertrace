"""Behaviour Analysis Engine (dossier §23–24, FR-5/FR-6).

Maintains per-service, per-metric, per-hour-of-week baselines and scores
new readings against the correct bucket. Three metrics are scored:

  cost_per_hour        the financial signal
  cpu_cores            the primary resource signal
  requests_per_second  the business signal, and the denominator of the
                       decoupling test

The detection condition is the conjunction from §24.2, not a single-metric
threshold: a resource or cost signal above 3 sigma *while traffic stays
unremarkable*. Traffic is scored so its Z-score can be compared, never so
that a high value alone triggers anything — heavy traffic is the
explanation for a spike, not evidence of one. When traffic moves with cost,
the reading is `legitimate_traffic_growth` and authorises nothing.

Two guards keep the detector honest, both biased toward false negatives
because the cost of a miss is money while the cost of a false positive is
an automatic action against a healthy service (§24.5):

  * Dwell — three consecutive qualifying samples before anything is
    flagged, so a scrape landing mid-garbage-collection cannot raise an
    incident on its own.
  * Suppression — a flagged reading never trains the baseline, or repeated
    incidents would teach the detector to accept them. Bounded, so a
    workload that has genuinely shifted is eventually re-baselined.

Scoring does no I/O: traffic arrives on its own topic and the latest value
per service is held in memory, so the hot path stays inside its latency
budget (§19.1).
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hypertrace_common.db import make_engine
from hypertrace_common.messaging import (
    ROUTING_KEY_ANOMALY,
    ROUTING_KEY_COST,
    ROUTING_KEY_TRAFFIC,
    RabbitMQClient,
)
from hypertrace_common.schemas import Anomaly, AnomalyClassification
from hypertrace_common.tables import anomalies, baselines

from .confidence import CORROBORATION_NONE, MATURITY_SAMPLES, Scores, confidence
from .stats import BucketStats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("behaviour-analysis")

METRIC_COST = "cost_per_hour"
METRIC_CPU = "cpu_cores"
METRIC_TRAFFIC = "requests_per_second"

# §24.2. Resource and cost share a threshold deliberately: cost is derived
# from resources, so a materially lower cost threshold would double-count
# the same underlying movement and make the conjunction weaker than it looks.
RESOURCE_Z_THRESHOLD = 3.0
COST_Z_THRESHOLD = 3.0
TRAFFIC_Z_CEILING = 1.0        # traffic must be unremarkable, not low
TRAFFIC_Z_COMOVEMENT = 3.0     # above this, traffic explains the movement
DWELL_SAMPLES = 3

MIN_SAMPLES_FOR_DETECTION = 5
OVERALL_KEY = "_overall"
FLAG_STATE_KEY = "_flag_state"
MAX_CONSECUTIVE_SUPPRESSED = 30

# Latest traffic Z-score per service, kept in memory so scoring performs no
# I/O. Absent means "no traffic signal", which is treated as unknown rather
# than as zero — see _traffic_z_for.
_traffic_z: dict[str, float] = {}
_traffic_lock = threading.Lock()


def _bucket_key(timestamp: datetime) -> str:
    return f"{timestamp.weekday()}-{timestamp.hour}"


def _should_learn(qualifies: bool, consecutive_flags: int) -> tuple[bool, int]:
    """Decides whether this reading may update the baseline.

    Takes `qualifies`, not `should_flag`: a reading that meets the detection
    condition but has not yet satisfied the dwell requirement is still a
    candidate anomaly, and must not train the baseline while it waits. Using
    `should_flag` here meant the baseline absorbed the first two samples of
    every incident, so the Z-score decayed before the third could confirm it
    — dwell quietly cancelled out detection for anything short of an
    enormous spike.

    Anomalous readings are excluded for the same reason more generally:
    folding them in raises the mean and inflates the stddev, so the next
    identical incident scores lower and eventually stops being flagged at
    all (observed: after two injected spikes a 470x cost jump scored 2.4).

    The escape hatch stops that freezing the baseline forever when a
    workload has genuinely shifted to a new normal.
    """
    if not qualifies:
        return True, 0
    if consecutive_flags >= MAX_CONSECUTIVE_SUPPRESSED:
        return True, 0
    return False, consecutive_flags + 1


def evaluate_condition(
    *, cost_z: float, cpu_z: float, traffic_z: float | None, mature: bool
) -> tuple[bool, bool]:
    """The detection conjunction from §24.2, as a pure function.

    Returns (qualifies, traffic_explains):

      qualifies        a resource/cost signal past threshold *while traffic
                       stays unremarkable* — the decoupling condition
      traffic_explains traffic moved with cost, so the movement is accounted
                       for and nothing should be authorised

    `traffic_z is None` means no traffic signal has arrived. That is not the
    same as no traffic: reading an absent signal as zero would make every
    service look decoupled and flag everything, so nothing qualifies until
    traffic is actually known.
    """
    if not mature or traffic_z is None:
        return False, False

    resource_moved = max(cost_z, cpu_z) > RESOURCE_Z_THRESHOLD and cost_z > COST_Z_THRESHOLD
    traffic_explains = resource_moved and traffic_z >= TRAFFIC_Z_COMOVEMENT
    decoupled = traffic_z < TRAFFIC_Z_CEILING
    return resource_moved and decoupled, traffic_explains


def _load_profile(engine, service: str, metric: str) -> dict[str, dict[str, float]]:
    stmt = select(baselines.c.day_of_week_profile).where(
        baselines.c.service == service, baselines.c.metric == metric
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return dict(row.day_of_week_profile) if row is not None else {}


def _save_profile(engine, service: str, metric: str, profile: dict[str, dict[str, float]]) -> None:
    overall = BucketStats.from_dict(profile.get(OVERALL_KEY, {}))
    stmt = pg_insert(baselines).values(
        service=service,
        metric=metric,
        rolling_mean=overall.mean,
        rolling_stddev=overall.stddev,
        day_of_week_profile=profile,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[baselines.c.service, baselines.c.metric],
        set_={
            "rolling_mean": stmt.excluded.rolling_mean,
            "rolling_stddev": stmt.excluded.rolling_stddev,
            "day_of_week_profile": stmt.excluded.day_of_week_profile,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    with engine.begin() as conn:
        conn.execute(stmt)


def _score_and_learn(engine, service: str, metric: str, value: float, bucket: str) -> tuple[float, int]:
    """Scores a value against its bucket and folds it into the baseline.

    Used for metrics that only inform the decision (CPU, traffic) rather
    than gate it, so suppression does not apply — those baselines should
    keep learning even while a cost anomaly is in progress.
    """
    profile = _load_profile(engine, service, metric)
    stats = BucketStats.from_dict(profile.get(bucket, {}))
    overall = BucketStats.from_dict(profile.get(OVERALL_KEY, {}))

    z = stats.z_score(value) if stats.n >= MIN_SAMPLES_FOR_DETECTION else 0.0

    stats.update(value)
    overall.update(value)
    profile[bucket] = stats.to_dict()
    profile[OVERALL_KEY] = overall.to_dict()
    _save_profile(engine, service, metric, profile)
    return z, stats.n


def _traffic_z_for(service: str) -> float | None:
    with _traffic_lock:
        return _traffic_z.get(service)


def _handle_traffic(engine, message: dict[str, Any]) -> None:
    service = message["service"]
    timestamp = datetime.fromisoformat(message["timestamp"])
    z, _n = _score_and_learn(
        engine, service, METRIC_TRAFFIC, message["requests_per_second"], _bucket_key(timestamp)
    )
    with _traffic_lock:
        _traffic_z[service] = z


def _flag_anomaly(
    engine,
    publish_mq: RabbitMQClient,
    *,
    service: str,
    scores: Scores,
    score_confidence: float,
    classification: AnomalyClassification,
    mature: bool,
    cost_delta: float,
    value: float,
    bucket: str,
    resource: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc)
    anomaly = Anomaly(
        id=str(uuid.uuid4()),
        service=service,
        score=scores.peak,
        classification=classification,
        evidence={
            "metric": METRIC_COST,
            "value": value,
            "bucket": bucket,
            "resource": resource,
            # The evidence the decision was made on, not a summary of it, so
            # the row alone is enough to reconstruct the verdict without
            # re-querying metrics that may since have been rolled up (§18.4).
            "scores": {"cost_z": scores.cost_z, "cpu_z": scores.cpu_z, "traffic_z": scores.traffic_z},
            "confidence": score_confidence,
            "baseline_mature": mature,
            "cost_delta_usd_hr": cost_delta,
        },
        status="open",
        created_at=now,
    )
    with engine.begin() as conn:
        conn.execute(
            anomalies.insert().values(
                id=anomaly.id,
                service=anomaly.service,
                score=anomaly.score,
                classification=anomaly.classification.value,
                evidence=anomaly.evidence,
                status=anomaly.status,
                created_at=anomaly.created_at,
                confidence=score_confidence,
                baseline_mature=mature,
                cost_delta_usd_hr=cost_delta,
            )
        )
    payload = anomaly.model_dump()
    payload["confidence"] = score_confidence
    publish_mq.publish(ROUTING_KEY_ANOMALY, payload)
    logger.warning(
        "ANOMALY service=%s class=%s cost_z=%.2f cpu_z=%.2f traffic_z=%.2f confidence=%.2f mature=%s",
        service,
        classification.value,
        scores.cost_z,
        scores.cpu_z,
        scores.traffic_z,
        score_confidence,
        mature,
    )


def _handle_cost(engine, publish_mq: RabbitMQClient, message: dict[str, Any]) -> None:
    service = message["service"]
    value = message["cost_per_hour"]
    timestamp = datetime.fromisoformat(message["timestamp"])
    bucket = _bucket_key(timestamp)

    cpu_z, _ = _score_and_learn(engine, service, METRIC_CPU, message.get("cpu_cores", 0.0), bucket)

    profile = _load_profile(engine, service, METRIC_COST)
    stats = BucketStats.from_dict(profile.get(bucket, {}))
    overall = BucketStats.from_dict(profile.get(OVERALL_KEY, {}))
    flag_state = profile.get(FLAG_STATE_KEY, {})

    mature = stats.n >= MIN_SAMPLES_FOR_DETECTION
    cost_z = stats.z_score(value) if mature else 0.0
    cost_delta = value - stats.mean if mature else 0.0

    traffic_z = _traffic_z_for(service)
    have_traffic = traffic_z is not None
    scores = Scores(cost_z=cost_z, cpu_z=cpu_z, traffic_z=traffic_z or 0.0)

    qualifies, traffic_explains = evaluate_condition(
        cost_z=cost_z, cpu_z=cpu_z, traffic_z=traffic_z, mature=mature
    )
    consecutive = int(flag_state.get("consecutive_qualifying", 0)) + 1 if qualifies else 0
    # Dwell: a single sample can be an artefact of a scrape landing
    # mid-garbage-collection (§24.2).
    should_flag = qualifies and consecutive >= DWELL_SAMPLES

    learn, suppressed = _should_learn(qualifies, int(flag_state.get("consecutive", 0)))
    if learn:
        stats.update(value)
        overall.update(value)
        profile[bucket] = stats.to_dict()
        profile[OVERALL_KEY] = overall.to_dict()
    profile[FLAG_STATE_KEY] = {"consecutive": suppressed, "consecutive_qualifying": consecutive}
    _save_profile(engine, service, METRIC_COST, profile)

    if should_flag:
        _flag_anomaly(
            engine,
            publish_mq,
            service=service,
            scores=scores,
            score_confidence=confidence(scores, CORROBORATION_NONE, stats.n),
            # decision-policy refines this once it has looked for a
            # corroborating deployment or security signal; the one verdict
            # decidable here is that traffic explained the movement.
            classification=(
                AnomalyClassification.LEGITIMATE_TRAFFIC_GROWTH
                if traffic_explains
                else AnomalyClassification.UNCLASSIFIED
            ),
            mature=stats.n >= MATURITY_SAMPLES,
            cost_delta=cost_delta,
            value=value,
            bucket=bucket,
            resource=message["resource"],
        )
    elif traffic_explains:
        logger.info(
            "service=%s cost_z=%.2f rose with traffic_z=%.2f — legitimate growth, no action",
            service,
            cost_z,
            traffic_z,
        )
    else:
        logger.info(
            "service=%s value=%.6f bucket=%s n=%d cost_z=%.2f cpu_z=%.2f traffic_z=%s dwell=%d/%d",
            service,
            value,
            bucket,
            stats.n,
            cost_z,
            cpu_z,
            f"{traffic_z:.2f}" if have_traffic else "none",
            consecutive,
            DWELL_SAMPLES,
        )


def main() -> None:
    engine = make_engine()
    publish_mq = RabbitMQClient()
    traffic_mq = RabbitMQClient()
    cost_mq = RabbitMQClient()

    threading.Thread(
        target=lambda: traffic_mq.consume(
            queue="behaviour-analysis.traffic",
            routing_keys=[ROUTING_KEY_TRAFFIC],
            on_message=lambda m: _handle_traffic(engine, m),
        ),
        daemon=True,
        name="traffic-consumer",
    ).start()

    logger.info(
        "behaviour-analysis starting: cost_z>%.1f cpu_z>%.1f traffic_z<%.1f dwell=%d",
        COST_Z_THRESHOLD,
        RESOURCE_Z_THRESHOLD,
        TRAFFIC_Z_CEILING,
        DWELL_SAMPLES,
    )
    cost_mq.consume(
        queue="behaviour-analysis.cost",
        routing_keys=[ROUTING_KEY_COST],
        on_message=lambda m: _handle_cost(engine, publish_mq, m),
    )


if __name__ == "__main__":
    main()
