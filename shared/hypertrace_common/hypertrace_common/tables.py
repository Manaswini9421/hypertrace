"""SQLAlchemy Core table definitions mirroring infra/sql/init.sql exactly.

Deliberately Core `Table` objects, not a full ORM model layer — table
creation/migration stays owned by init.sql (applied once by the DB
container's docker-entrypoint-initdb.d), these are just typed handles for
building queries/inserts. Requires the `db` extra.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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

actions_log = Table(
    "actions_log",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("anomaly_id", UUID(as_uuid=True)),
    Column("action_type", Text, nullable=False),
    Column("executed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("result", Text, nullable=False),
    Column("rollback_ref", Text),
)
