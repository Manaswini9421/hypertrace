"""Integration tests against the real TimescaleDB instance.

These cover what a stubbed SQLAlchemy cannot: that the schema in
infra/sql/init.sql actually matches what the services write, that the
hypertables accept and range-query time-series rows, that JSONB evidence
survives a round trip, and that behaviour-analysis's baseline upsert
(a real ON CONFLICT DO UPDATE) behaves across restarts.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from hypertrace_common.tables import actions_log, anomalies, baselines, cost_events


class TestSchema:
    def test_every_table_from_the_data_model_exists(self, db_engine):
        """doc Section 5.2 lists six entities; a missing one means a service
        would fail at runtime, not at deploy time.
        """
        expected = {"raw_metrics", "cost_events", "baselines", "anomalies", "policies", "actions_log"}
        with db_engine.connect() as conn:
            found = {
                row[0]
                for row in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
        assert expected <= found, f"missing tables: {expected - found}"

    def test_timeseries_tables_are_hypertables(self, db_engine):
        """If create_hypertable silently failed, these would still accept
        writes but lose TimescaleDB's partitioning — a silent regression.
        """
        with db_engine.connect() as conn:
            found = {
                row[0]
                for row in conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables"))
            }
        assert {"cost_events", "raw_metrics"} <= found


class TestCostEvents:
    def test_write_and_range_query(self, db_engine, unique_service, db_cleanup):
        """The exact shape of the api-bff's cost endpoint query."""
        db_cleanup(unique_service)
        now = datetime.now(timezone.utc)
        with db_engine.begin() as conn:
            for minutes_ago, cost in ((30, 0.10), (10, 0.20), (1, 0.30)):
                conn.execute(
                    cost_events.insert().values(
                        time=now - timedelta(minutes=minutes_ago),
                        service=unique_service,
                        resource_type="compute",
                        unit_rate=0.032,
                        cost_per_hour=cost,
                    )
                )

        since = now - timedelta(minutes=15)
        stmt = (
            select(cost_events.c.cost_per_hour)
            .where(cost_events.c.service == unique_service, cost_events.c.time >= since)
            .order_by(cost_events.c.time)
        )
        with db_engine.connect() as conn:
            costs = [row.cost_per_hour for row in conn.execute(stmt)]

        assert costs == [0.20, 0.30], "range filter should exclude the 30-minute-old row"


class TestAnomalies:
    def test_jsonb_evidence_round_trips(self, db_engine, unique_service, db_cleanup):
        """Evidence is the audit record for why something was flagged
        (FR-9), including nested resource detail — it must survive intact.
        """
        db_cleanup(unique_service)
        evidence = {
            "metric": "cost_per_hour",
            "z_score": 16068.22,
            "bucket": "0-9",
            "resource": {"namespace": "itest", "pod": "itest-pod"},
            "classification_reason": {"security_rule": "unexpected_outbound_connection"},
        }
        anomaly_id = uuid.uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id,
                    service=unique_service,
                    score=16068.22,
                    classification="suspected_abuse",
                    evidence=evidence,
                    status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
            row = conn.execute(select(anomalies).where(anomalies.c.id == anomaly_id)).one()

        assert row.evidence == evidence
        assert row.evidence["classification_reason"]["security_rule"] == "unexpected_outbound_connection"

    def test_decision_policy_can_reclassify_in_place(self, db_engine, unique_service, db_cleanup):
        """behaviour-analysis writes `unclassified`; decision-policy updates
        it and merges its reason into the existing evidence.
        """
        db_cleanup(unique_service)
        anomaly_id = uuid.uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id,
                    service=unique_service,
                    score=9.0,
                    classification="unclassified",
                    evidence={"metric": "cost_per_hour", "z_score": 9.0},
                    status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
            conn.execute(
                anomalies.update()
                .where(anomalies.c.id == anomaly_id)
                .values(
                    classification="likely_bug_from_deployment",
                    evidence={"metric": "cost_per_hour", "z_score": 9.0, "classification_reason": {"recent_deployment": True}},
                )
            )
            row = conn.execute(select(anomalies).where(anomalies.c.id == anomaly_id)).one()

        assert row.classification == "likely_bug_from_deployment"
        assert row.evidence["z_score"] == 9.0, "the original detection evidence must be preserved"
        assert row.evidence["classification_reason"]["recent_deployment"] is True


class TestBaselinePersistence:
    def test_upsert_survives_a_restart(self, db_engine, unique_service, db_cleanup):
        """Drives behaviour-analysis's real _save_profile/_load_profile against
        the real DB. This is the path that makes baselines outlive a pod
        restart — the property bug 3 was about.
        """
        db_cleanup(unique_service)
        from svc_behaviour.main import METRIC_COST, OVERALL_KEY, _load_profile, _save_profile
        from svc_behaviour.stats import BucketStats

        stats = BucketStats()
        for value in (0.001, 0.002, 0.003):
            stats.update(value)
        profile = {"0-9": stats.to_dict(), OVERALL_KEY: stats.to_dict()}
        _save_profile(db_engine, unique_service, METRIC_COST, profile)

        # A second save must update the existing row, not raise on the
        # composite primary key or silently insert a duplicate.
        stats.update(0.004)
        profile["0-9"] = stats.to_dict()
        profile[OVERALL_KEY] = stats.to_dict()
        _save_profile(db_engine, unique_service, METRIC_COST, profile)

        reloaded = _load_profile(db_engine, unique_service, METRIC_COST)
        assert BucketStats.from_dict(reloaded["0-9"]).n == 4, "state should accumulate across saves"

        with db_engine.connect() as conn:
            count = conn.execute(
                select(func.count()).select_from(baselines).where(baselines.c.service == unique_service)
            ).scalar_one()
        assert count == 1, "upsert must not create duplicate baseline rows"

    def test_missing_baseline_reads_as_empty(self, db_engine, unique_service):
        """A never-seen service must start clean rather than raising — the
        cold-start path from doc 11.2.
        """
        from svc_behaviour.main import METRIC_COST, _load_profile

        assert _load_profile(db_engine, unique_service, METRIC_COST) == {}


class TestAuditLog:
    def test_decision_and_outcome_are_separate_rows(self, db_engine, unique_service, db_cleanup):
        """decision-policy records the decision; the executor appends the
        outcome pointing back at it (dossier §21.4). Collapsing these into
        one mutated row would erase the record of what was requested.
        """
        db_cleanup(unique_service)
        anomaly_id, decision_id, outcome_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id, service=unique_service, score=5.0,
                    classification="suspected_abuse", evidence={}, status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
            conn.execute(
                actions_log.insert().values(
                    id=decision_id, anomaly_id=anomaly_id, action_type="throttle",
                    mode="autonomous", executed_at=datetime.now(timezone.utc),
                    result="dispatched", target={"service": unique_service},
                )
            )
            conn.execute(
                actions_log.insert().values(
                    id=outcome_id, anomaly_id=anomaly_id, parent_action_id=decision_id,
                    action_type="throttle", mode="autonomous",
                    executed_at=datetime.now(timezone.utc), result="executed",
                    target={"service": unique_service},
                    prior_state={"kind": "deployment_cpu_limit", "previous_cpu_limit": "1"},
                    rollback_deadline=datetime.now(timezone.utc) + timedelta(minutes=60),
                )
            )
            rows = conn.execute(
                select(actions_log).where(actions_log.c.anomaly_id == anomaly_id)
                .order_by(actions_log.c.executed_at)
            ).all()

        assert [r.result for r in rows] == ["dispatched", "executed"]
        assert rows[1].parent_action_id == decision_id, "the outcome must point back at its decision"
        assert rows[1].prior_state["previous_cpu_limit"] == "1"
        assert rows[1].rollback_deadline is not None

    def test_the_database_refuses_to_update_the_ledger(self, db_engine, unique_service, db_cleanup):
        """§21.4 puts this guarantee in the database, not in convention, so
        an application bug cannot quietly rewrite history.
        """
        from sqlalchemy.exc import InternalError, ProgrammingError

        db_cleanup(unique_service)
        anomaly_id, action_id = uuid.uuid4(), uuid.uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id, service=unique_service, score=5.0,
                    classification="suspected_abuse", evidence={}, status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
            conn.execute(
                actions_log.insert().values(
                    id=action_id, anomaly_id=anomaly_id, action_type="throttle",
                    mode="autonomous", executed_at=datetime.now(timezone.utc), result="executed",
                )
            )

        with pytest.raises((InternalError, ProgrammingError), match="append-only"):
            with db_engine.begin() as conn:
                conn.execute(
                    actions_log.update().where(actions_log.c.id == action_id).values(result="tampered")
                )

    def test_a_blocked_action_stores_sql_null_not_json_null(self, db_engine, unique_service, db_cleanup):
        """SQLAlchemy's JSON default turns Python None into the JSON literal
        `null`, which is not SQL NULL — so "which actions are reversible?"
        would silently include ones that were never applied.
        """
        db_cleanup(unique_service)
        anomaly_id, action_id = uuid.uuid4(), uuid.uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id, service=unique_service, score=5.0,
                    classification="misconfiguration_or_waste", evidence={}, status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
            conn.execute(
                actions_log.insert().values(
                    id=action_id, anomaly_id=anomaly_id, action_type="throttle",
                    mode="autonomous", executed_at=datetime.now(timezone.utc),
                    result="blocked_by_protected_floor", prior_state=None,
                )
            )
            row = conn.execute(select(actions_log).where(actions_log.c.id == action_id)).one()

        assert row.prior_state is None

    def test_rollback_is_a_separate_row(self, db_engine, unique_service, db_cleanup):
        """actions_log is append-only evidence: "we throttled, then undid it"
        must remain two legible events, not one mutated row.
        """
        db_cleanup(unique_service)
        anomaly_id = uuid.uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id, service=unique_service, score=5.0,
                    classification="suspected_abuse", evidence={}, status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
            for action_type, result in (("throttle", "executed"), ("rollback", "rolled_back")):
                conn.execute(
                    actions_log.insert().values(
                        id=uuid.uuid4(), anomaly_id=anomaly_id, action_type=action_type,
                        executed_at=datetime.now(timezone.utc), result=result,
                    )
                )
            rows = conn.execute(
                select(actions_log.c.action_type, actions_log.c.result)
                .where(actions_log.c.anomaly_id == anomaly_id)
                .order_by(actions_log.c.executed_at)
            ).all()

        assert [(r.action_type, r.result) for r in rows] == [
            ("throttle", "executed"),
            ("rollback", "rolled_back"),
        ]

    def test_orphan_actions_are_rejected(self, db_engine):
        """The FK to anomalies is what stops the audit log accumulating
        actions with no traceable cause.
        """
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            with db_engine.begin() as conn:
                conn.execute(
                    actions_log.insert().values(
                        id=uuid.uuid4(), anomaly_id=uuid.uuid4(), action_type="throttle",
                        executed_at=datetime.now(timezone.utc), result="executed",
                    )
                )
