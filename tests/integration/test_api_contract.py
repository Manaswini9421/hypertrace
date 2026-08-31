"""Integration tests against the running api-bff.

These are the contract the React dashboard depends on, and they cover the
security boundaries that only exist server-side: the frontend hiding a
button is UX, the 403 here is the actual control (FR-14).
"""

import uuid

import httpx
import pytest

TIMEOUT = 15.0
DEV_PASSWORD = "hypertrace-dev"


def _login(api_base: str, username: str) -> str:
    response = httpx.post(
        f"{api_base}/api/v1/auth/login",
        data={"username": username, "password": DEV_PASSWORD},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.fixture(scope="session")
def sre_token(api_base):
    return _login(api_base, "sre")


@pytest.fixture(scope="session")
def finance_token(api_base):
    return _login(api_base, "finance")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    def test_healthz_needs_no_auth(self, api_base):
        response = httpx.get(f"{api_base}/healthz", timeout=TIMEOUT)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_login_issues_a_token_carrying_the_role(self, api_base):
        import base64
        import json

        token = _login(api_base, "sre")
        # The dashboard reads the role straight out of the payload to decide
        # which controls to render, so the claim has to be there.
        payload_b64 = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        assert payload["sub"] == "sre"
        assert payload["role"] == "sre"

    def test_bad_password_is_rejected(self, api_base):
        response = httpx.post(
            f"{api_base}/api/v1/auth/login",
            data={"username": "sre", "password": "wrong"},
            timeout=TIMEOUT,
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("path", ["/api/v1/dashboard/summary", "/api/v1/anomalies", "/api/v1/actions"])
    def test_reads_require_a_token(self, api_base, path):
        assert httpx.get(f"{api_base}{path}", timeout=TIMEOUT).status_code == 401

    def test_a_forged_token_is_rejected(self, api_base):
        response = httpx.get(
            f"{api_base}/api/v1/anomalies", headers=_auth("not.a.real.token"), timeout=TIMEOUT
        )
        assert response.status_code == 401


class TestReadContracts:
    def test_dashboard_summary_shape(self, api_base, sre_token):
        body = httpx.get(
            f"{api_base}/api/v1/dashboard/summary", headers=_auth(sre_token), timeout=TIMEOUT
        ).json()
        assert isinstance(body["total_cost_per_hour"], (int, float))
        assert isinstance(body["top_services"], list)
        assert len(body["top_services"]) <= 10
        for entry in body["top_services"]:
            assert set(entry) == {"service", "cost_per_hour"}

    def test_summary_total_is_consistent_with_its_breakdown(self, api_base, sre_token):
        """The headline figure the dashboard shows must not disagree with the
        list printed directly beneath it.
        """
        body = httpx.get(
            f"{api_base}/api/v1/dashboard/summary", headers=_auth(sre_token), timeout=TIMEOUT
        ).json()
        listed = sum(e["cost_per_hour"] for e in body["top_services"])
        # top_services is capped at 10, so the total is >= their sum.
        assert body["total_cost_per_hour"] >= listed - 1e-9

    def test_anomalies_shape_and_limit(self, api_base, sre_token):
        body = httpx.get(
            f"{api_base}/api/v1/anomalies?limit=5", headers=_auth(sre_token), timeout=TIMEOUT
        ).json()
        assert len(body) <= 5
        for anomaly in body:
            assert {"id", "service", "score", "classification", "evidence", "status", "created_at"} <= set(anomaly)

    def test_actions_shape(self, api_base, sre_token):
        body = httpx.get(
            f"{api_base}/api/v1/actions?limit=5", headers=_auth(sre_token), timeout=TIMEOUT
        ).json()
        for action in body:
            assert {"id", "anomaly_id", "action_type", "executed_at", "result", "rollback_ref"} <= set(action)


class TestRoleBasedAccess:
    """FR-14: a finance user reads cost data but cannot change anything."""

    def test_finance_can_read_cost_data(self, api_base, finance_token):
        response = httpx.get(
            f"{api_base}/api/v1/dashboard/summary", headers=_auth(finance_token), timeout=TIMEOUT
        )
        assert response.status_code == 200

    def test_finance_cannot_create_a_policy(self, api_base, finance_token):
        response = httpx.post(
            f"{api_base}/api/v1/policies",
            headers=_auth(finance_token),
            json={"action": "throttle", "rule_dsl": {}},
            timeout=TIMEOUT,
        )
        assert response.status_code == 403

    def test_finance_cannot_trigger_remediation(self, api_base, finance_token):
        response = httpx.post(
            f"{api_base}/api/v1/actions/{uuid.uuid4()}/rollback",
            headers=_auth(finance_token),
            timeout=TIMEOUT,
        )
        assert response.status_code == 403, "role must be checked before the anomaly is even looked up"


class TestPolicyLifecycle:
    def test_create_list_delete(self, api_base, sre_token):
        rule = {"service_prefix": f"itest-{uuid.uuid4().hex[:8]}/", "min_cost_per_hour": 99.0}
        created = httpx.post(
            f"{api_base}/api/v1/policies",
            headers=_auth(sre_token),
            json={"org_id": "itest", "scope": "*", "priority": 1, "action": "alert_only", "rule_dsl": rule},
            timeout=TIMEOUT,
        )
        assert created.status_code == 201
        policy_id = created.json()["id"]

        try:
            listed = httpx.get(f"{api_base}/api/v1/policies", headers=_auth(sre_token), timeout=TIMEOUT).json()
            assert any(p["id"] == policy_id and p["rule_dsl"] == rule for p in listed)
        finally:
            deleted = httpx.delete(
                f"{api_base}/api/v1/policies/{policy_id}", headers=_auth(sre_token), timeout=TIMEOUT
            )
            assert deleted.status_code == 204

        remaining = httpx.get(f"{api_base}/api/v1/policies", headers=_auth(sre_token), timeout=TIMEOUT).json()
        assert not any(p["id"] == policy_id for p in remaining)

    def test_deleting_an_unknown_policy_is_a_404(self, api_base, sre_token):
        response = httpx.delete(
            f"{api_base}/api/v1/policies/{uuid.uuid4()}", headers=_auth(sre_token), timeout=TIMEOUT
        )
        assert response.status_code == 404


class TestRemediationGuards:
    def test_rollback_without_an_executed_action_is_a_404(self, api_base, sre_token):
        """A rollback request for an anomaly that was never acted on must not
        invent an action to undo.
        """
        response = httpx.post(
            f"{api_base}/api/v1/actions/{uuid.uuid4()}/rollback", headers=_auth(sre_token), timeout=TIMEOUT
        )
        assert response.status_code == 404

    def test_approve_without_a_pending_action_is_a_404(self, api_base, sre_token):
        response = httpx.post(
            f"{api_base}/api/v1/actions/{uuid.uuid4()}/approve", headers=_auth(sre_token), timeout=TIMEOUT
        )
        assert response.status_code == 404


class TestAlertStream:
    """The WebSocket carries a JWT as a query parameter, because the browser
    WebSocket API cannot set headers on the handshake — so the handshake
    itself must enforce auth.
    """

    _HANDSHAKE = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    }

    def test_valid_token_upgrades(self, api_base, sre_token):
        response = httpx.get(
            f"{api_base}/api/v1/stream/alerts?token={sre_token}", headers=self._HANDSHAKE, timeout=TIMEOUT
        )
        assert response.status_code == 101

    def test_invalid_token_is_refused(self, api_base):
        response = httpx.get(
            f"{api_base}/api/v1/stream/alerts?token=garbage", headers=self._HANDSHAKE, timeout=TIMEOUT
        )
        assert response.status_code == 403

    def test_missing_token_is_refused(self, api_base):
        response = httpx.get(
            f"{api_base}/api/v1/stream/alerts", headers=self._HANDSHAKE, timeout=TIMEOUT
        )
        assert response.status_code == 403
