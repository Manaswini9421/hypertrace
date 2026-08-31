"""Integration tests for the Remediation Executor against a real cluster.

Everything here operates on a **disposable Deployment the test creates and
deletes itself** — never on the demo workloads. That is what makes it safe
to run against the live cluster while still proving the patches actually
land on a real API server, which the fake-client unit tests cannot.

Also asserts the executor's RBAC is genuinely restrictive (NFR-4, and the
answer to defense-kit Q4: "you're giving a monitoring tool the power to
kill production workloads"). Those checks use SubjectAccessReview, which
asks the API server what the ServiceAccount is permitted to do — so they
verify the deployed policy rather than the YAML's intent.
"""

import json
import time
import uuid

import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from svc_remediation.k8s_actions import THROTTLE_CPU_LIMIT, rollback, throttle

NAMESPACE = "hypertrace"
ORIGINAL_CPU = "500m"


@pytest.fixture(scope="session")
def k8s():
    """API clients from the local kubeconfig, skipping if no cluster."""
    try:
        k8s_config.load_kube_config()
    except Exception:
        pytest.skip("no kubeconfig available")

    apps = k8s_client.AppsV1Api()
    try:
        apps.list_namespaced_deployment(NAMESPACE, limit=1)
    except Exception as exc:
        pytest.skip(f"cluster not reachable: {exc}")

    return k8s_client.CoreV1Api(), apps, k8s_client.AutoscalingV2Api(), k8s_client.AuthorizationV1Api()


@pytest.fixture
def disposable_deployment(k8s):
    """Creates a paused, zero-replica Deployment for the test to act on.

    replicas=0 keeps it free and instant: the executor patches the pod
    template, which is what these tests assert on, and no pod ever needs to
    schedule. Deleted afterwards even if the test fails.
    """
    _core, apps, _autoscaling, _auth = k8s
    name = f"itest-target-{uuid.uuid4().hex[:8]}"

    apps.create_namespaced_deployment(
        NAMESPACE,
        k8s_client.V1Deployment(
            metadata=k8s_client.V1ObjectMeta(name=name, labels={"app": name, "itest": "true"}),
            spec=k8s_client.V1DeploymentSpec(
                replicas=0,
                selector=k8s_client.V1LabelSelector(match_labels={"app": name}),
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(labels={"app": name}),
                    spec=k8s_client.V1PodSpec(
                        containers=[
                            k8s_client.V1Container(
                                name=name,
                                image="registry.k8s.io/pause:3.9",
                                resources=k8s_client.V1ResourceRequirements(limits={"cpu": ORIGINAL_CPU}),
                            )
                        ]
                    ),
                ),
            ),
        ),
    )
    yield name

    try:
        apps.delete_namespaced_deployment(name, NAMESPACE)
    except k8s_client.ApiException:
        pass


def _cpu_limit(apps, name: str) -> str | None:
    deployment = apps.read_namespaced_deployment(name, NAMESPACE)
    limits = deployment.spec.template.spec.containers[0].resources.limits or {}
    return limits.get("cpu")


class TestThrottleAgainstRealCluster:
    def test_throttle_actually_patches_the_deployment(self, k8s, disposable_deployment):
        core, apps, _autoscaling, _auth = k8s
        name = disposable_deployment
        assert _cpu_limit(apps, name) == ORIGINAL_CPU

        result = throttle(apps, core, f"{NAMESPACE}/{name}")

        assert result["status"] == "executed"
        assert _cpu_limit(apps, name) == THROTTLE_CPU_LIMIT, "the real Deployment should now be capped"
        assert json.loads(result["rollback_ref"])["previous_cpu_limit"] == ORIGINAL_CPU

    def test_throttle_then_rollback_restores_the_original(self, k8s, disposable_deployment):
        """The full FR-11 round trip against a real API server — the property
        the dashboard's Roll back button depends on.
        """
        core, apps, autoscaling, _auth = k8s
        name = disposable_deployment

        throttled = throttle(apps, core, f"{NAMESPACE}/{name}")
        assert _cpu_limit(apps, name) == THROTTLE_CPU_LIMIT

        result = rollback(apps, autoscaling, throttled["rollback_ref"])

        assert result["status"] == "rolled_back"
        assert _cpu_limit(apps, name) == ORIGINAL_CPU, "rollback must restore exactly the original limit"

    def test_throttle_is_idempotent_against_a_real_deployment(self, k8s, disposable_deployment):
        """The live pipeline dispatches repeatedly while an incident lasts.
        A second patch would overwrite the rollback reference with 100m and
        lose the original limit permanently.
        """
        core, apps, _autoscaling, _auth = k8s
        name = disposable_deployment

        first = throttle(apps, core, f"{NAMESPACE}/{name}")
        second = throttle(apps, core, f"{NAMESPACE}/{name}")

        assert first["status"] == "executed"
        assert second["status"] == "no_op"
        assert second["rollback_ref"] is None
        assert json.loads(first["rollback_ref"])["previous_cpu_limit"] == ORIGINAL_CPU

    def test_missing_deployment_is_a_no_op(self, k8s):
        core, apps, _autoscaling, _auth = k8s
        result = throttle(apps, core, f"{NAMESPACE}/itest-does-not-exist-{uuid.uuid4().hex[:6]}")

        assert result["status"] == "no_op"


class TestExecutorRBAC:
    """Verifies the deployed RBAC, not the YAML's intent.

    SubjectAccessReview asks the API server directly what the executor's
    ServiceAccount may do. If someone widens the Role later, these fail.
    """

    SERVICE_ACCOUNT = f"system:serviceaccount:{NAMESPACE}:remediation-executor"

    def _can(self, auth_api, verb: str, resource: str, group: str = "") -> bool:
        review = auth_api.create_subject_access_review(
            k8s_client.V1SubjectAccessReview(
                spec=k8s_client.V1SubjectAccessReviewSpec(
                    user=self.SERVICE_ACCOUNT,
                    resource_attributes=k8s_client.V1ResourceAttributes(
                        namespace=NAMESPACE, verb=verb, resource=resource, group=group
                    ),
                )
            )
        )
        return bool(review.status.allowed)

    @pytest.mark.parametrize("verb,resource,group", [("get", "deployments", "apps"), ("patch", "deployments", "apps")])
    def test_can_do_what_it_needs(self, k8s, verb, resource, group):
        _core, _apps, _autoscaling, auth = k8s
        assert self._can(auth, verb, resource, group), f"executor must be able to {verb} {resource}"

    @pytest.mark.parametrize(
        "verb,resource,group",
        [
            ("delete", "pods", ""),          # quarantine/terminate are out of scope
            ("delete", "deployments", "apps"),
            ("get", "secrets", ""),          # nothing here needs credentials
            ("create", "pods", ""),
            ("patch", "nodes", ""),
        ],
    )
    def test_cannot_do_anything_else(self, k8s, verb, resource, group):
        """The claim behind defense-kit Q4: a bug or compromise in HyperTrace
        cannot delete workloads or read secrets, because the API server will
        not let it.
        """
        _core, _apps, _autoscaling, auth = k8s
        assert not self._can(auth, verb, resource, group), f"executor must NOT be able to {verb} {resource}"

    def test_cannot_touch_protected_namespaces(self, k8s):
        """The Role is namespace-scoped, so the kube-system floor is enforced
        by Kubernetes itself — not only by the executor's own config check.
        """
        _core, _apps, _autoscaling, auth = k8s
        review = auth.create_subject_access_review(
            k8s_client.V1SubjectAccessReview(
                spec=k8s_client.V1SubjectAccessReviewSpec(
                    user=self.SERVICE_ACCOUNT,
                    resource_attributes=k8s_client.V1ResourceAttributes(
                        namespace="kube-system", verb="patch", resource="deployments", group="apps"
                    ),
                )
            )
        )
        assert not review.status.allowed, "executor must not be able to patch kube-system"


class TestRateLimit:
    """The blast-radius cap from doc 11.3: a bug upstream must not cascade
    into unbounded changes across the cluster.
    """

    def test_counts_only_recent_executed_actions(self, db_engine, unique_service, db_cleanup):
        from datetime import datetime, timedelta, timezone

        from svc_remediation import config as exec_config
        from svc_remediation.main import _rate_limit_exceeded
        from hypertrace_common.tables import actions_log, anomalies

        db_cleanup(unique_service)
        anomaly_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=exec_config.RATE_LIMIT_WINDOW_MINUTES + 5)

        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id, service=unique_service, score=5.0,
                    classification="misconfiguration_or_waste", evidence={}, status="open", created_at=now,
                )
            )
            # Outside the window, so these must not count toward the limit.
            for _ in range(exec_config.MAX_ACTIONS_PER_WINDOW + 2):
                conn.execute(
                    actions_log.insert().values(
                        id=uuid.uuid4(), anomaly_id=anomaly_id, action_type="throttle",
                        executed_at=old, result="executed",
                    )
                )

        assert not _rate_limit_exceeded(db_engine), "actions outside the window must not trip the limit"

    def test_no_ops_do_not_count_toward_the_limit(self, db_engine, unique_service, db_cleanup):
        """A repeatedly-dispatched incident produces many no_ops. If those
        counted, one noisy incident would exhaust the budget and block
        genuine remediation elsewhere.
        """
        from datetime import datetime, timezone

        from svc_remediation import config as exec_config
        from svc_remediation.main import _rate_limit_exceeded
        from hypertrace_common.tables import actions_log, anomalies

        db_cleanup(unique_service)
        anomaly_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        with db_engine.begin() as conn:
            conn.execute(
                anomalies.insert().values(
                    id=anomaly_id, service=unique_service, score=5.0,
                    classification="misconfiguration_or_waste", evidence={}, status="open", created_at=now,
                )
            )
            for _ in range(exec_config.MAX_ACTIONS_PER_WINDOW + 2):
                conn.execute(
                    actions_log.insert().values(
                        id=uuid.uuid4(), anomaly_id=anomaly_id, action_type="throttle",
                        executed_at=now, result="no_op",
                    )
                )

        assert not _rate_limit_exceeded(db_engine), "no_op actions changed nothing and must not count"
