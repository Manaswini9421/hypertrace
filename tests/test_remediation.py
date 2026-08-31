"""Unit tests for the Remediation Executor's Kubernetes actions.

These drive the real `k8s_actions` functions against a fake client, so they
cover the decision logic — what gets patched, what is refused, what the
rollback reference records — without needing a cluster. The counterpart in
tests/integration/test_remediation_k8s.py proves the same code actually
mutates a real Deployment.

This is the component that holds cluster write credentials, so its
"decline to act" paths matter as much as its "act" ones: a wrong no_op
costs an incident, but a wrong patch costs a workload.
"""

import json

import pytest
from kubernetes import client as k8s_client

from svc_remediation.k8s_actions import THROTTLE_CPU_LIMIT, freeze_scaling, rollback, throttle


class FakeResources:
    def __init__(self, cpu_limit=None):
        self.limits = {"cpu": cpu_limit} if cpu_limit else {}


class FakeContainer:
    def __init__(self, name="app", cpu_limit=None):
        self.name = name
        self.resources = FakeResources(cpu_limit)


class FakeDeployment:
    def __init__(self, name="web", cpu_limit="1"):
        self.metadata = type("Meta", (), {"name": name})()
        container = FakeContainer(name=name, cpu_limit=cpu_limit)
        self.spec = type("Spec", (), {
            "template": type("Tmpl", (), {"spec": type("PodSpec", (), {"containers": [container]})()})()
        })()


class FakeAppsV1:
    """Records patches instead of applying them."""

    def __init__(self, deployment: FakeDeployment | None = None, missing: bool = False):
        self._deployment = deployment or FakeDeployment()
        self._missing = missing
        self.patches: list[tuple[str, str, dict]] = []

    def read_namespaced_deployment(self, name, namespace):
        if self._missing:
            raise k8s_client.ApiException(status=404, reason="Not Found")
        return self._deployment

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))


class FakeHPA:
    def __init__(self, name="web-hpa", target="web", max_replicas=10, current_replicas=4):
        self.metadata = type("Meta", (), {"name": name})()
        self.spec = type("Spec", (), {
            "max_replicas": max_replicas,
            "scale_target_ref": type("Ref", (), {"name": target})(),
        })()
        self.status = type("Status", (), {"current_replicas": current_replicas})()


class FakeAutoscalingV2:
    def __init__(self, hpas=None):
        self._hpas = hpas if hpas is not None else []
        self.patches: list[tuple[str, str, dict]] = []

    def list_namespaced_horizontal_pod_autoscaler(self, namespace):
        return type("List", (), {"items": self._hpas})()

    def patch_namespaced_horizontal_pod_autoscaler(self, name, namespace, body):
        self.patches.append((name, namespace, body))


class TestThrottle:
    def test_caps_cpu_and_records_how_to_undo_it(self):
        apps = FakeAppsV1(FakeDeployment(name="web", cpu_limit="1"))
        result = throttle(apps, core_v1=None, service="hypertrace/web")

        assert result["status"] == "executed"
        name, namespace, body = apps.patches[0]
        assert (name, namespace) == ("web", "hypertrace")
        patched = body["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"]
        assert patched == THROTTLE_CPU_LIMIT

        ref = json.loads(result["rollback_ref"])
        assert ref["previous_cpu_limit"] == "1", "must record the value needed to restore it"
        assert ref["deployment"] == "web"

    def test_is_idempotent(self):
        """A duplicate dispatch must not re-patch. If it did, the rollback
        reference would record 100m as the "previous" limit and the real
        original would be lost forever.
        """
        apps = FakeAppsV1(FakeDeployment(cpu_limit=THROTTLE_CPU_LIMIT))
        result = throttle(apps, core_v1=None, service="hypertrace/web")

        assert result["status"] == "no_op"
        assert result["rollback_ref"] is None
        assert apps.patches == [], "an already-throttled workload must not be patched again"

    def test_missing_deployment_is_a_no_op_not_a_crash(self):
        """Pods outlive their Deployment. A stale anomaly must not take the
        executor down or leave the action unrecorded.
        """
        apps = FakeAppsV1(missing=True)
        result = throttle(apps, core_v1=None, service="hypertrace/deleted")

        assert result["status"] == "no_op"
        assert "no Deployment named deleted" in result["reason"]

    def test_a_deployment_with_no_limit_is_still_throttled(self):
        """An unlimited workload is exactly the one worth capping, and its
        rollback must restore "no limit" rather than inventing one.
        """
        apps = FakeAppsV1(FakeDeployment(cpu_limit=None))
        result = throttle(apps, core_v1=None, service="hypertrace/web")

        assert result["status"] == "executed"
        assert json.loads(result["rollback_ref"])["previous_cpu_limit"] is None

    def test_api_errors_other_than_404_propagate(self):
        """A 403 means the executor's RBAC is wrong. Swallowing it as a
        no_op would hide a broken deployment behind a clean audit log.
        """
        class Forbidden(FakeAppsV1):
            def read_namespaced_deployment(self, name, namespace):
                raise k8s_client.ApiException(status=403, reason="Forbidden")

        with pytest.raises(k8s_client.ApiException):
            throttle(Forbidden(), core_v1=None, service="hypertrace/web")


class TestFreezeScaling:
    def test_pins_max_replicas_to_the_current_count(self):
        autoscaling = FakeAutoscalingV2([FakeHPA(target="web", max_replicas=10, current_replicas=4)])
        result = freeze_scaling(autoscaling, apps_v1=None, core_v1=None, service="hypertrace/web")

        assert result["status"] == "executed"
        _name, _ns, body = autoscaling.patches[0]
        assert body["spec"]["maxReplicas"] == 4, "freeze should stop growth at today's size"
        assert json.loads(result["rollback_ref"])["previous_max_replicas"] == 10

    def test_no_hpa_means_nothing_to_freeze(self):
        autoscaling = FakeAutoscalingV2([])
        result = freeze_scaling(autoscaling, apps_v1=None, core_v1=None, service="hypertrace/web")

        assert result["status"] == "no_op"
        assert autoscaling.patches == []

    def test_ignores_hpas_targeting_other_workloads(self):
        """Freezing a bystander's autoscaler because it happened to live in
        the same namespace would be a serious blast-radius failure.
        """
        autoscaling = FakeAutoscalingV2([FakeHPA(name="other-hpa", target="other")])
        result = freeze_scaling(autoscaling, apps_v1=None, core_v1=None, service="hypertrace/web")

        assert result["status"] == "no_op"
        assert autoscaling.patches == []

    def test_already_frozen_is_a_no_op(self):
        autoscaling = FakeAutoscalingV2([FakeHPA(target="web", max_replicas=4, current_replicas=4)])
        result = freeze_scaling(autoscaling, apps_v1=None, core_v1=None, service="hypertrace/web")

        assert result["status"] == "no_op"
        assert autoscaling.patches == []

    def test_scaled_to_zero_is_left_alone(self):
        """A workload at zero replicas is not scaling and not costing
        anything. Pinning maxReplicas at 0 would freeze it permanently, and
        pinning it at 1 would block a legitimate future scale-up — so the
        only safe answer is to do nothing.
        """
        autoscaling = FakeAutoscalingV2([FakeHPA(target="web", max_replicas=10, current_replicas=0)])
        result = freeze_scaling(autoscaling, apps_v1=None, core_v1=None, service="hypertrace/web")

        assert result["status"] == "no_op"
        assert "scaled to zero" in result["reason"]
        assert autoscaling.patches == []

    def test_unreported_replica_count_is_left_alone(self):
        """A freshly-created HPA has no status yet. Guessing a count from
        maxReplicas would freeze the workload at a number it never reached.
        """
        autoscaling = FakeAutoscalingV2([FakeHPA(target="web", max_replicas=10, current_replicas=None)])
        result = freeze_scaling(autoscaling, apps_v1=None, core_v1=None, service="hypertrace/web")

        assert result["status"] == "no_op"
        assert autoscaling.patches == []


class TestRollback:
    def test_restores_the_previous_cpu_limit(self):
        apps = FakeAppsV1()
        ref = json.dumps({
            "kind": "deployment_cpu_limit", "namespace": "hypertrace",
            "deployment": "web", "container": "web", "previous_cpu_limit": "1",
        })
        result = rollback(apps, autoscaling_v2=None, rollback_ref_json=ref)

        assert result["status"] == "rolled_back"
        body = apps.patches[0][2]
        assert body["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "1"

    def test_restores_the_previous_max_replicas(self):
        autoscaling = FakeAutoscalingV2()
        ref = json.dumps({
            "kind": "hpa_max_replicas", "namespace": "hypertrace",
            "hpa": "web-hpa", "previous_max_replicas": 10,
        })
        result = rollback(apps_v1=None, autoscaling_v2=autoscaling, rollback_ref_json=ref)

        assert result["status"] == "rolled_back"
        assert autoscaling.patches[0][2]["spec"]["maxReplicas"] == 10

    def test_unknown_reference_fails_loudly(self):
        """A reference the executor cannot interpret must report failure, not
        silently claim the rollback succeeded.
        """
        result = rollback(apps_v1=None, autoscaling_v2=None, rollback_ref_json=json.dumps({"kind": "future_action"}))

        assert result["status"] == "failed"
        assert "future_action" in result["reason"]

    def test_round_trip_returns_the_original_limit(self):
        """throttle's reference must be exactly what rollback consumes —
        the two halves have to agree on the format.
        """
        apps = FakeAppsV1(FakeDeployment(name="web", cpu_limit="2"))
        throttled = throttle(apps, core_v1=None, service="hypertrace/web")
        rollback(apps, autoscaling_v2=None, rollback_ref_json=throttled["rollback_ref"])

        restored = apps.patches[-1][2]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"]
        assert restored == "2"


class TestProtectedFloor:
    """The executor re-checks the protected floor even though decision-policy
    already did, because it is the component holding the credentials
    (doc 11.3, defense in depth).
    """

    def test_refuses_protected_namespaces(self):
        from svc_remediation.main import _is_protected

        assert _is_protected("kube-system/kube-proxy")

    def test_allows_application_namespaces(self):
        from svc_remediation.main import _is_protected

        assert not _is_protected("hypertrace/victim")
