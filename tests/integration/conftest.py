"""Fixtures for integration tests.

Unlike tests/ (pure logic, infrastructure stubbed), these run against real
RabbitMQ, real TimescaleDB, and the real API — the layers where every bug
found during live testing actually lived. They exercise the shipped modules
(`RabbitMQClient`, behaviour-analysis's baseline persistence) rather than
reimplementing their behaviour.

They need the services reachable on localhost:

    kubectl -n hypertrace port-forward svc/rabbitmq     5672:5672
    kubectl -n hypertrace port-forward svc/timescaledb  5432:5432
    kubectl -n hypertrace port-forward svc/api-bff     18000:8000

or `make integration`, which sets those up and tears them down. Any test
whose backing service is unreachable is skipped, not failed — a missing
port-forward is an environment gap, not a regression.

Everything written here is namespaced under an `itest-` prefix and cleaned
up afterwards, so these are safe to run against the live demo cluster
without polluting the data an operator is looking at.
"""

import os
import socket
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "shared" / "hypertrace_common"))

RABBITMQ_HOST = os.environ.get("ITEST_RABBITMQ_HOST", "127.0.0.1")
RABBITMQ_PORT = int(os.environ.get("ITEST_RABBITMQ_PORT", "5672"))
DATABASE_HOST = os.environ.get("ITEST_DATABASE_HOST", "127.0.0.1")
DATABASE_PORT = int(os.environ.get("ITEST_DATABASE_PORT", "5432"))
API_BASE = os.environ.get("ITEST_API_BASE", "http://127.0.0.1:18000")

TEST_PREFIX = "itest-"


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _require(host: str, port: int, name: str, hint: str) -> None:
    if not _reachable(host, port):
        pytest.skip(f"{name} not reachable at {host}:{port} — run: {hint}")


@pytest.fixture(scope="session")
def mq_settings():
    _require(
        RABBITMQ_HOST, RABBITMQ_PORT, "RabbitMQ",
        "kubectl -n hypertrace port-forward svc/rabbitmq 5672:5672",
    )
    from hypertrace_common.config import RabbitMQSettings

    return RabbitMQSettings(host=RABBITMQ_HOST, port=RABBITMQ_PORT)


@pytest.fixture(scope="session")
def db_engine():
    _require(
        DATABASE_HOST, DATABASE_PORT, "TimescaleDB",
        "kubectl -n hypertrace port-forward svc/timescaledb 5432:5432",
    )
    from hypertrace_common.config import DatabaseSettings
    from hypertrace_common.db import make_engine

    return make_engine(DatabaseSettings(host=DATABASE_HOST, port=DATABASE_PORT))


@pytest.fixture(scope="session")
def api_base():
    host, _, port = API_BASE.rsplit(":", 1)[0].rpartition("//")[2], None, API_BASE.rsplit(":", 1)[1]
    _require(
        host, int(port), "api-bff",
        "kubectl -n hypertrace port-forward svc/api-bff 18000:8000",
    )
    return API_BASE


@pytest.fixture
def unique_service() -> str:
    """A service id no real workload will ever collide with, so assertions
    about "rows for this service" stay deterministic on a live cluster that
    is continuously writing its own data.
    """
    return f"{TEST_PREFIX}{uuid.uuid4().hex[:12]}"


def _delete_matching(engine, where_clause: str, params: dict) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        # actions_log is append-only, enforced by a database trigger. Tests
        # connect as the table owner and disable it deliberately to tidy up
        # after themselves; nothing in the application can do this, which is
        # the point of the trigger.
        conn.execute(text("ALTER TABLE actions_log DISABLE TRIGGER actions_log_no_delete"))
        try:
            conn.execute(
                text(
                    "DELETE FROM actions_log WHERE anomaly_id IN "
                    f"(SELECT id FROM anomalies WHERE {where_clause})"
                ),
                params,
            )
        finally:
            conn.execute(text("ALTER TABLE actions_log ENABLE TRIGGER actions_log_no_delete"))
        for table in ("anomalies", "baselines", "cost_events"):
            conn.execute(text(f"DELETE FROM {table} WHERE {where_clause}"), params)


@pytest.fixture
def db_cleanup(db_engine):
    """Removes every row this test wrote, keyed by the service id, even if
    the test fails partway through.
    """
    services: list[str] = []
    yield services.append

    for service in services:
        _delete_matching(db_engine, "service = :s", {"s": service})


@pytest.fixture(scope="session", autouse=True)
def final_sweep():
    """Deletes any `itest-` rows left over once the whole session is done.

    Per-test cleanup runs the moment a test finishes, but the pipeline it
    just fed is still asynchronously processing — cost-intelligence and
    behaviour-analysis keep writing rows for the test's workload for a few
    seconds afterwards, landing *after* the delete. Without this sweep the
    demo cluster slowly accumulates synthetic services in its dashboards.
    """
    yield

    if not _reachable(DATABASE_HOST, DATABASE_PORT):
        return

    import time

    from hypertrace_common.config import DatabaseSettings
    from hypertrace_common.db import make_engine

    time.sleep(3)  # let in-flight pipeline writes land before sweeping
    engine = make_engine(DatabaseSettings(host=DATABASE_HOST, port=DATABASE_PORT))
    _delete_matching(engine, "service LIKE :p", {"p": f"%{TEST_PREFIX}%"})
