-- Schema for the core entities in docs/report.html Section 5.2 "Data model".
-- Applied automatically by the TimescaleDB image's docker-entrypoint-initdb.d
-- mechanism, both in docker-compose.dev.yml and the in-cluster ConfigMap at
-- infra/k8s/postgres-timescaledb/timescaledb.yaml. Keep the two copies in
-- sync if you change this file.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- raw_metrics: FR-1/FR-2 telemetry, written by the Phase 2 Cost Intelligence
-- Engine as it consumes MetricEvent messages off RabbitMQ.
CREATE TABLE IF NOT EXISTS raw_metrics (
    time                      TIMESTAMPTZ NOT NULL,
    cluster                   TEXT NOT NULL,
    namespace                 TEXT NOT NULL,
    node                      TEXT NOT NULL,
    pod                       TEXT,
    container                 TEXT,
    service                   TEXT,
    cpu_usage_cores           DOUBLE PRECISION,
    memory_working_set_bytes  BIGINT,
    network_rx_bytes_total    BIGINT,
    network_tx_bytes_total    BIGINT,
    disk_read_bytes_total     BIGINT,
    disk_write_bytes_total    BIGINT
);
SELECT create_hypertable('raw_metrics', 'time', if_not_exists => TRUE);

-- cost_events: FR-3/FR-4, written by the Phase 2 Cost Intelligence Engine.
CREATE TABLE IF NOT EXISTS cost_events (
    time                    TIMESTAMPTZ NOT NULL,
    service                 TEXT NOT NULL,
    resource_type           TEXT NOT NULL,
    unit_rate               DOUBLE PRECISION NOT NULL,
    cost_per_hour           DOUBLE PRECISION NOT NULL,
    cost_per_unit_of_work   DOUBLE PRECISION
);
SELECT create_hypertable('cost_events', 'time', if_not_exists => TRUE);

-- baselines: FR-5, maintained by the Phase 3 Behaviour Analysis Engine.
CREATE TABLE IF NOT EXISTS baselines (
    service               TEXT NOT NULL,
    metric                TEXT NOT NULL,
    rolling_mean          DOUBLE PRECISION NOT NULL,
    rolling_stddev        DOUBLE PRECISION NOT NULL,
    day_of_week_profile   JSONB NOT NULL DEFAULT '{}',
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, metric)
);

-- anomalies: FR-6, written by the Phase 3 Behaviour Analysis Engine.
CREATE TABLE IF NOT EXISTS anomalies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service         TEXT NOT NULL,
    score           DOUBLE PRECISION NOT NULL,
    classification  TEXT NOT NULL DEFAULT 'unclassified',
    evidence        JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- policies: FR-7/FR-10, managed by the Phase 4 Decision & Policy Engine.
CREATE TABLE IF NOT EXISTS policies (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      TEXT NOT NULL,
    rule_dsl    JSONB NOT NULL,
    scope       TEXT NOT NULL,
    action      TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0
);

-- actions_log: FR-9/FR-11, append-only audit trail written by the Phase 4
-- Remediation Executor. Never updated or deleted, only inserted.
CREATE TABLE IF NOT EXISTS actions_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    anomaly_id      UUID REFERENCES anomalies (id),
    action_type     TEXT NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    result          TEXT NOT NULL,
    rollback_ref    TEXT
);
