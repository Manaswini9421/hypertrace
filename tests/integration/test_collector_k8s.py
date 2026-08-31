"""Integration tests for the collector against a real cluster.

Read-only throughout — the collector only ever observes, so nothing here
creates or mutates anything.

The most valuable test is the first one: bug 2 was that the Kubernetes
Python client returned the kubelet's stats payload as a Python `repr()`
string instead of JSON. Only a real API server reproduces that, and it
crash-looped the DaemonSet on every node.
"""

import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from svc_collector.kubelet_client import fetch_stats_summary, parse_node_metrics, parse_pod_metrics
from svc_collector.workload_resolver import WorkloadResolver

NAMESPACE = "hypertrace"


@pytest.fixture(scope="session")
def k8s():
    try:
        k8s_config.load_kube_config()
    except Exception:
        pytest.skip("no kubeconfig available")

    core = k8s_client.CoreV1Api()
    try:
        core.list_node(limit=1)
    except Exception as exc:
        pytest.skip(f"cluster not reachable: {exc}")

    return core, k8s_client.AppsV1Api(), k8s_client.AuthorizationV1Api()


@pytest.fixture(scope="session")
def a_node(k8s):
    """A node that actually runs a collector pod.

    Not simply the first node: kind taints the control-plane, so the
    DaemonSet does not schedule there and its stats contain none of our
    workloads — which would make the assertions below vacuous.
    """
    core, _apps, _auth = k8s
    collectors = core.list_namespaced_pod(NAMESPACE, label_selector="app=collector").items
    scheduled = [p.spec.node_name for p in collectors if p.spec.node_name]
    if not scheduled:
        pytest.skip("no collector pods are scheduled")
    return scheduled[0]


@pytest.fixture(scope="session")
def real_summary(k8s, a_node):
    core, _apps, _auth = k8s
    return fetch_stats_summary(core, a_node)


class TestKubeletStats:
    def test_the_real_kubelet_payload_parses(self, real_summary):
        """Bug 2's regression guard. Before the fix this raised
        JSONDecodeError against a real API server, and no fake reproduced it.
        """
        assert isinstance(real_summary, dict)
        assert "node" in real_summary
        assert "pods" in real_summary

    def test_node_metrics_are_plausible(self, real_summary):
        parsed = parse_node_metrics(real_summary)

        assert parsed["cpu_usage_cores"] > 0, "a running node is always using some CPU"
        assert parsed["cpu_usage_cores"] < 1000, "a nanocore/core conversion error would show up as an absurd value"
        assert parsed["memory_working_set_bytes"] > 0

    def test_pod_metrics_are_returned_for_real_pods(self, real_summary):
        pods = parse_pod_metrics(real_summary)

        assert pods, "a node running the hypertrace stack must report pods"
        for pod in pods:
            assert pod["pod"], "every returned entry must be attributable to a named pod"
            assert pod["namespace"]
            assert pod["cpu_usage_cores"] >= 0

    def test_the_collector_itself_appears_in_its_own_metrics(self, real_summary):
        """Sanity check that we are reading the node we think we are."""
        names = [p["pod"] for p in parse_pod_metrics(real_summary)]
        assert any(name.startswith("collector-") for name in names)


class TestWorkloadResolutionAgainstRealObjects:
    def test_resolves_a_deployment_pod_to_its_deployment(self, k8s):
        """Walks a real Pod -> ReplicaSet -> Deployment ownership chain."""
        core, apps, _auth = k8s
        pods = core.list_namespaced_pod(NAMESPACE, label_selector="app=api-bff").items
        if not pods:
            pytest.skip("api-bff is not running")

        resolved = WorkloadResolver(core, apps).resolve(NAMESPACE, pods[0].metadata.name)

        assert resolved == "api-bff"
        assert resolved != pods[0].metadata.name, "identity must not be the pod name"

    def test_resolves_a_daemonset_pod_to_its_daemonset(self, k8s):
        """DaemonSet pods have no ReplicaSet in the chain."""
        core, apps, _auth = k8s
        pods = core.list_namespaced_pod(NAMESPACE, label_selector="app=collector").items
        if not pods:
            pytest.skip("collector is not running")

        assert WorkloadResolver(core, apps).resolve(NAMESPACE, pods[0].metadata.name) == "collector"

    def test_every_replica_resolves_to_the_same_workload(self, k8s):
        """The invariant behind bug 3, checked against real pods: however
        many replicas exist, they are one billable service.
        """
        core, apps, _auth = k8s
        pods = core.list_namespaced_pod(NAMESPACE, label_selector="app=collector").items
        if len(pods) < 2:
            pytest.skip("need at least two collector pods")

        resolver = WorkloadResolver(core, apps)
        resolved = {resolver.resolve(NAMESPACE, p.metadata.name) for p in pods}

        assert resolved == {"collector"}, f"replicas disagreed on identity: {resolved}"

    def test_an_unknown_pod_degrades_to_its_own_name(self, k8s):
        """Pods disappear between listing and resolving. That must not stop
        the collection cycle.
        """
        core, apps, _auth = k8s

        assert WorkloadResolver(core, apps).resolve(NAMESPACE, "itest-nonexistent-pod") == "itest-nonexistent-pod"


class TestCollectorRBAC:
    """The collector observes and never acts, so its ServiceAccount should
    have no write access anywhere. Verified through SubjectAccessReview
    against the deployed policy rather than the YAML's intent.
    """

    SERVICE_ACCOUNT = f"system:serviceaccount:{NAMESPACE}:collector"

    def _can(self, auth_api, verb, resource, group="", namespace=NAMESPACE):
        review = auth_api.create_subject_access_review(
            k8s_client.V1SubjectAccessReview(
                spec=k8s_client.V1SubjectAccessReviewSpec(
                    user=self.SERVICE_ACCOUNT,
                    resource_attributes=k8s_client.V1ResourceAttributes(
                        namespace=namespace, verb=verb, resource=resource, group=group
                    ),
                )
            )
        )
        return bool(review.status.allowed)

    @pytest.mark.parametrize(
        "verb,resource,group",
        [
            ("list", "pods", ""),
            ("watch", "events", ""),
            ("get", "nodes", ""),
            ("get", "replicasets", "apps"),   # needed for ownership resolution
            ("get", "deployments", "apps"),
        ],
    )
    def test_can_read_what_it_observes(self, k8s, verb, resource, group):
        _core, _apps, auth = k8s
        assert self._can(auth, verb, resource, group), f"collector must be able to {verb} {resource}"

    @pytest.mark.parametrize(
        "verb,resource,group",
        [
            ("patch", "deployments", "apps"),
            ("delete", "pods", ""),
            ("create", "pods", ""),
            ("get", "secrets", ""),
            ("patch", "nodes", ""),
        ],
    )
    def test_has_no_write_access_at_all(self, k8s, verb, resource, group):
        """A read-only agent that could write would undermine the whole
        least-privilege argument (NFR-4) — the collector runs on every node.
        """
        _core, _apps, auth = k8s
        assert not self._can(auth, verb, resource, group), f"collector must NOT be able to {verb} {resource}"
