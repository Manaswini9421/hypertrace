"""Behaviour Analysis Engine entrypoint (doc Section 3 Phase 3, FR-5/FR-6).

Consumes CostEvent messages and maintains a per-service, per-hour-of-week
rolling baseline — the incremental equivalent of doc 14.2's
"mean, stddev = rolling_stats(service_metric_history, window='14d',
bucket='same_hour_of_week')" — via Welford's online algorithm (stats.py),
persisted to the `baselines` table so it survives restarts. A reading more
than Z_THRESHOLD standard deviations from its bucket's mean is flagged into
the `anomalies` table and published for downstream consumers.

Classification here is a placeholder ("unclassified"): doc 14.3's joint
reasoning across cost/traffic/security signals is the Phase 4 Decision &
Policy Engine's job, which will consume these same anomaly.flagged events.
Below MIN_SAMPLES_FOR_DETECTION for a bucket, readings still update the
baseline but never flag — the cold-start mitigation from doc 11.2 (a
service's first samples in a given hour-of-week slot have nothing to be
"anomalous" relative to yet).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hypertrace_common.db import make_engine
from hypertrace_common.messaging import ROUTING_KEY_ANOMALY, ROUTING_KEY_COST, RabbitMQClient
from hypertrace_common.schemas import Anomaly, AnomalyClassification
from hypertrace_common.tables import anomalies, baselines

from .stats import BucketStats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("behaviour-analysis")

METRIC_NAME = "cost_per_hour"
Z_THRESHOLD = 3.0
MIN_SAMPLES_FOR_DETECTION = 5
OVERALL_KEY = "_overall"  # combined-across-all-buckets entry, stored alongside the hour-of-week buckets
FLAG_STATE_KEY = "_flag_state"  # consecutive-flag counter, stored alongside the buckets

# After this many consecutive flagged readings, the engine stops suppressing
# and folds the elevated values into the baseline — see _should_learn.
MAX_CONSECUTIVE_SUPPRESSED = 30


def _bucket_key(timestamp: datetime) -> str:
    return f"{timestamp.weekday()}-{timestamp.hour}"


def _should_learn(should_flag: bool, consecutive_flags: int) -> tuple[bool, int]:
    """Decides whether this reading may update the baseline, and returns the
    new consecutive-flag count.

    Anomalous readings are excluded from the baseline. Folding them in is
    self-defeating: each incident raises the mean and inflates the stddev,
    so the next identical incident scores a lower z and eventually stops
    being flagged at all. A workload that misbehaves repeatedly — exactly
    the recurring-cryptominer case in doc 11.5 — would train the detector
    to accept it. (Observed directly: after two injected spikes, a 470x
    cost jump scored only z=2.4 and went undetected.)

    The escape hatch stops that from freezing the baseline forever when a
    workload has genuinely, permanently shifted to a new normal (real
    traffic growth). After MAX_CONSECUTIVE_SUPPRESSED consecutive flags we
    accept the new level and resume learning, so the system re-baselines
    instead of alerting indefinitely.
    """
    if not should_flag:
        return True, 0
    if consecutive_flags >= MAX_CONSECUTIVE_SUPPRESSED:
        return True, 0
    return False, consecutive_flags + 1


def _load_profile(engine, service: str) -> dict[str, dict[str, float]]:
    stmt = select(baselines.c.day_of_week_profile).where(
        baselines.c.service == service, baselines.c.metric == METRIC_NAME
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return dict(row.day_of_week_profile) if row is not None else {}


def _save_profile(engine, service: str, profile: dict[str, dict[str, float]]) -> None:
    overall = BucketStats.from_dict(profile.get(OVERALL_KEY, {}))
    now = datetime.now(timezone.utc)
    stmt = pg_insert(baselines).values(
        service=service,
        metric=METRIC_NAME,
        rolling_mean=overall.mean,
        rolling_stddev=overall.stddev,
        day_of_week_profile=profile,
        updated_at=now,
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


def _flag_anomaly(
    engine,
    publish_mq: RabbitMQClient,
    service: str,
    value: float,
    z: float,
    bucket: str,
    resource: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc)
    anomaly = Anomaly(
        id=str(uuid.uuid4()),
        service=service,
        score=z,
        classification=AnomalyClassification.UNCLASSIFIED,
        evidence={
            "metric": METRIC_NAME,
            "value": value,
            "z_score": z,
            "bucket": bucket,
            "resource": resource,
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
            )
        )
    publish_mq.publish(ROUTING_KEY_ANOMALY, anomaly.model_dump())
    logger.warning("ANOMALY service=%s metric=%s value=%.6f z=%.2f bucket=%s", service, METRIC_NAME, value, z, bucket)


def main() -> None:
    engine = make_engine()
    consume_mq = RabbitMQClient()
    publish_mq = RabbitMQClient()

    def handle(message: dict[str, Any]) -> None:
        service = message["service"]
        value = message["cost_per_hour"]
        timestamp = datetime.fromisoformat(message["timestamp"])
        bucket = _bucket_key(timestamp)

        profile = _load_profile(engine, service)
        bucket_stats = BucketStats.from_dict(profile.get(bucket, {}))
        overall_stats = BucketStats.from_dict(profile.get(OVERALL_KEY, {}))

        z = bucket_stats.z_score(value) if bucket_stats.n >= MIN_SAMPLES_FOR_DETECTION else 0.0
        should_flag = bucket_stats.n >= MIN_SAMPLES_FOR_DETECTION and abs(z) > Z_THRESHOLD

        consecutive = int(profile.get(FLAG_STATE_KEY, {}).get("consecutive", 0))
        learn, consecutive = _should_learn(should_flag, consecutive)

        if learn:
            bucket_stats.update(value)
            overall_stats.update(value)
            profile[bucket] = bucket_stats.to_dict()
            profile[OVERALL_KEY] = overall_stats.to_dict()
        profile[FLAG_STATE_KEY] = {"consecutive": consecutive}
        _save_profile(engine, service, profile)

        if should_flag:
            _flag_anomaly(engine, publish_mq, service, value, z, bucket, message["resource"])
        else:
            logger.info(
                "service=%s value=%.6f bucket=%s n=%d z=%.2f (below threshold or learning)",
                service,
                value,
                bucket,
                bucket_stats.n,
                z,
            )

    logger.info("behaviour-analysis starting: z_threshold=%s min_samples=%s", Z_THRESHOLD, MIN_SAMPLES_FOR_DETECTION)
    consume_mq.consume(
        queue="behaviour-analysis.cost",
        routing_keys=[ROUTING_KEY_COST],
        on_message=handle,
    )


if __name__ == "__main__":
    main()
