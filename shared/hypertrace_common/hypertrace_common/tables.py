"""SQLAlchemy Core table definitions mirroring infra/sql/init.sql exactly.

Deliberately Core `Table` objects, not a full ORM model layer — table
creation/migration stays owned by init.sql (applied once by the DB
container's docker-entrypoint-initdb.d), these are just typed handles for
building queries/inserts. Requires the `db` extra.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Double,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

metadata = MetaData()

raw_metrics = Table(
    "raw_metrics",
    metadata,
    Column("time", TIMESTAMP(timezone=True), nullable=False),
    Column("cluster", Text, nullable=False),
    Column("namespace", Text, nullable=False),
    Column("node", Text, nullable=False),
    Column("pod", Text),
    Column("container", Text),
    Column("service", Text),
    Column("cpu_usage_cores", Double),
    Column("memory_working_set_bytes", BigInteger),
    Column("network_rx_bytes_total", BigInteger),
    Column("network_tx_bytes_total", BigInteger),
    Column("disk_read_bytes_total", BigInteger),
    Column("disk_write_bytes_total", BigInteger),
)

cost_events = Table(
    "cost_events",
    metadata,
    Column("time", TIMESTAMP(timezone=True), nullable=False),
    Column("service", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("unit_rate", Double, nullable=False),
    Column("cost_per_hour", Double, nullable=False),
    Column("cost_per_unit_of_work", Double),
)

baselines = Table(
    "baselines",
    metadata,
    Column("service", Text, primary_key=True),
    Column("metric", Text, primary_key=True),
    Column("rolling_mean", Double, nullable=False),
    Column("rolling_stddev", Double, nullable=False),
    Column("day_of_week_profile", JSONB, nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
)

anomalies = Table(
    "anomalies",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("service", Text, nullable=False),
    Column("score", Double, nullable=False),
    Column("classification", Text, nullable=False),
    Column("evidence", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    # Authority is a function of confidence and only of confidence
    # (dossier §24.3), so it is stored rather than recomputed — the point is
    # that the decision can be audited afterwards.
    Column("confidence", Double),
    Column("baseline_mature", Boolean, nullable=False),
    Column("cost_delta_usd_hr", Double),
)

policies = Table(
    "policies",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("rule_dsl", JSONB, nullable=False),
    Column("scope", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("priority", Integer, nullable=False),
)

# Append-only ledger (dossier §21.4), enforced by database triggers — see
# infra/sql/002_append_only_audit.sql. Every state change is a new row:
# decision-policy writes the decision, the executor writes the outcome
# pointing back via parent_action_id, and a rollback is another row
# pointing at the row it reverses via rollback_ref. Never UPDATE this
# table; the database will reject it.
actions_log = Table(
    "actions_log",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("anomaly_id", UUID(as_uuid=True)),
    Column("parent_action_id", UUID(as_uuid=True)),
    Column("rollback_ref", UUID(as_uuid=True)),
    Column("action_type", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("executed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("result", Text, nullable=False),
    # none_as_null=True because SQLAlchemy otherwise stores a Python None as
    # the JSON literal `null`, which is not SQL NULL: a blocked action with
    # no prior state then satisfies `prior_state IS NOT NULL`, so any query
    # asking "which actions are reversible?" silently includes ones that
    # were never applied.
    Column("target", JSONB(none_as_null=True)),
    Column("prior_state", JSONB(none_as_null=True)),
    Column("applied_state", JSONB(none_as_null=True)),
    Column("rollback_deadline", TIMESTAMP(timezone=True)),
)
