"""Unit tests for the collector agent.

Covers the two places bugs actually appeared in this service: parsing the
kubelet's stats payload (bug 2 — the client returned a Python `repr()`
string that `json.loads` could not parse) and resolving a pod to a stable
workload identity (bug 3 — pod-name identity meant a restart orphaned the
baseline that had just detected an incident).

Both are regression-tested here against fakes; the counterparts in
tests/integration/test_collector_k8s.py run against the real kubelet.
"""

import json

import pytest

from svc_collector.kubelet_client import fetch_stats_summary, parse_node_metrics, parse_pod_metrics
from svc_collector.workload_resolver import WorkloadResolver

NANOCORES_PER_CORE = 1e9


def _summary(**overrides):
    base = {
        "node": {
            "cpu": {"usageNanoCores": 500_000_000},
            "memory": {"workingSetBytes": 1_000_000, "rssBytes": 800_000},
            "network": {"rxBytes": 10, "txBytes": 20},
        },
        "pods": [],
    }
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode("utf-8")


class FakeCoreV1:
    """Mimics the generated client, including the quirk behind bug 2.

    With content preloading on, the client deserialises the JSON and then
    re-stringifies it with Python's `repr()` because the endpoint declares a
    `str` return type — producing single-quoted output that `json.loads`
    rejects. Only `_preload_content=False` yields the real bytes.
    """

    def __init__(self, payload):
        self._payload = payload
        self.last_kwargs = None

    def connect_get_node_proxy_with_path(self, node, path, **kwargs):
        self.last_kwargs = kwargs
        if not kwargs.get("_preload_content", True):
            return FakeResponse(self._payload)
        return repr(self._payload)


class TestFetchStatsSummary:
    def test_parses_the_kubelet_payload(self):
        core = FakeCoreV1(_summary())
        result = fetch_stats_summary(core, "node-1")
        assert result["node"]["cpu"]["usageNanoCores"] == 500_000_000

    def test_requests_raw_bytes(self):
        """The regression guard for bug 2. Without _preload_content=False the
        client hands back a repr() string and parsing fails at runtime — the
        collector crash-looped on this.
        """
        core = FakeCoreV1(_summary())
        fetch_stats_summary(core, "node-1")
        assert core.last_kwargs.get("_preload_content") is False


class TestParseNodeMetrics:
    def test_converts_nanocores_to_cores(self):
        parsed = parse_node_metrics(_summary())
        assert parsed["cpu_usage_cores"] == pytest.approx(0.5)

    def test_carries_memory_and_network_through(self):
        parsed = parse_node_metrics(_summary())
        assert parsed["memory_working_set_bytes"] == 1_000_000
        assert parsed["memory_rss_bytes"] == 800_000
        assert (parsed["network_rx_bytes_total"], parsed["network_tx_bytes_total"]) == (10, 20)

    def test_absent_fields_do_not_raise(self):
        """A node reporting no network stats must not take the collector
        down — it is a DaemonSet, so one bad payload would crash-loop a node.
        """
        parsed = parse_node_metrics({"node": {}})
        assert parsed["cpu_usage_cores"] == 0
        assert parsed["memory_working_set_bytes"] == 0
        assert parsed["network_rx_bytes_total"] is None


class TestParsePodMetrics:
    def test_sums_usage_across_containers(self):
        """A pod's cost is the whole pod's, so a sidecar's CPU has to be
        included or multi-container workloads are systematically underpriced.
        """
        summary = _summary(pods=[{
            "podRef": {"name": "web", "namespace": "shop"},
            "containers": [
                {"cpu": {"usageNanoCores": 100_000_000}, "memory": {"workingSetBytes": 5_000}},
                {"cpu": {"usageNanoCores": 400_000_000}, "memory": {"workingSetBytes": 3_000}},
            ],
            "network": {"rxBytes": 1, "txBytes": 2},
        }])
        pod = parse_pod_metrics(summary)[0]
        assert pod["cpu_usage_cores"] == pytest.approx(0.5)
        assert pod["memory_working_set_bytes"] == 8_000
        assert (pod["namespace"], pod["pod"]) == ("shop", "web")

    def test_skips_entries_with_no_pod_name(self):
        """The kubelet occasionally reports entries with an empty podRef.
        Publishing those would create a metric attributed to nothing.
        """
        summary = _summary(pods=[
            {"podRef": {"name": "", "namespace": "shop"}, "containers": []},
            {"podRef": {"name": "web", "namespace": "shop"}, "containers": []},
        ])
        assert [p["pod"] for p in parse_pod_metrics(summary)] == ["web"]

    def test_a_pod_with_no_containers_reports_zero_not_an_error(self):
        summary = _summary(pods=[{"podRef": {"name": "pending", "namespace": "shop"}, "containers": []}])
        pod = parse_pod_metrics(summary)[0]
        assert pod["cpu_usage_cores"] == 0
        assert pod["memory_working_set_bytes"] == 0

    def test_no_pods_key_yields_nothing(self):
        assert parse_pod_metrics({"node": {}}) == []


class FakeMeta:
    def __init__(self, name, owner_references=None):
        self.name = name
        self.owner_references = owner_references or []


class FakeOwner:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name


class FakeOwned:
    def __init__(self, name, owner_references=None):
        self.metadata = FakeMeta(name, owner_references)


class FakeCoreForResolver:
    def __init__(self, pods: dict):
        self._pods = pods
        self.reads = 0

    def read_namespaced_pod(self, name, namespace):
        self.reads += 1
        if name not in self._pods:
            raise RuntimeError("not found")
        return self._pods[name]


class FakeAppsForResolver:
    def __init__(self, replica_sets: dict | None = None):
        self._replica_sets = replica_sets or {}

    def read_namespaced_replica_set(self, name, namespace):
        return self._replica_sets[name]


class TestWorkloadResolver:
    """Identity must survive a pod restart, including restarts HyperTrace
    itself causes when it throttles a Deployment (bug 3).
    """

    def test_resolves_a_deployment_pod_through_its_replicaset(self):
        core = FakeCoreForResolver({"web-7d9f-abcde": FakeOwned("web-7d9f-abcde", [FakeOwner("ReplicaSet", "web-7d9f")])})
        apps = FakeAppsForResolver({"web-7d9f": FakeOwned("web-7d9f", [FakeOwner("Deployment", "web")])})

        assert WorkloadResolver(core, apps).resolve("shop", "web-7d9f-abcde") == "web"

    def test_two_pods_of_one_deployment_share_an_identity(self):
        """The property the whole fix exists for: a restart produces a new
        pod name but must not produce a new service.
        """
        core = FakeCoreForResolver({
            "web-7d9f-aaaaa": FakeOwned("web-7d9f-aaaaa", [FakeOwner("ReplicaSet", "web-7d9f")]),
            "web-8e0a-bbbbb": FakeOwned("web-8e0a-bbbbb", [FakeOwner("ReplicaSet", "web-8e0a")]),
        })
        apps = FakeAppsForResolver({
            "web-7d9f": FakeOwned("web-7d9f", [FakeOwner("Deployment", "web")]),
            "web-8e0a": FakeOwned("web-8e0a", [FakeOwner("Deployment", "web")]),
        })
        resolver = WorkloadResolver(core, apps)

        assert resolver.resolve("shop", "web-7d9f-aaaaa") == resolver.resolve("shop", "web-8e0a-bbbbb") == "web"

    def test_daemonset_pods_resolve_directly(self):
        """DaemonSets and StatefulSets own their pods without a ReplicaSet,
        so the owner's name is already the stable identity.
        """
        core = FakeCoreForResolver({"collector-x9": FakeOwned("collector-x9", [FakeOwner("DaemonSet", "collector")])})

        assert WorkloadResolver(core, FakeAppsForResolver()).resolve("hypertrace", "collector-x9") == "collector"

    def test_bare_pods_fall_back_to_their_own_name(self):
        """A pod with no controller genuinely has no stabler identity."""
        core = FakeCoreForResolver({"standalone": FakeOwned("standalone", [])})

        assert WorkloadResolver(core, FakeAppsForResolver()).resolve("shop", "standalone") == "standalone"

    def test_lookup_failure_degrades_instead_of_raising(self):
        """A resolver error must not stop metric collection — losing identity
        precision is far better than losing the metrics entirely.
        """
        resolver = WorkloadResolver(FakeCoreForResolver({}), FakeAppsForResolver())

        assert resolver.resolve("shop", "vanished") == "vanished"

    def test_results_are_cached(self):
        """This runs for every pod on every 10s cycle; re-resolving each time
        would put avoidable load on the API server (NFR-2).
        """
        core = FakeCoreForResolver({"web-1": FakeOwned("web-1", [FakeOwner("DaemonSet", "web")])})
        resolver = WorkloadResolver(core, FakeAppsForResolver())

        for _ in range(5):
            resolver.resolve("shop", "web-1")

        assert core.reads == 1, "ownership barely changes, so it should be looked up once"

    def test_prune_drops_pods_that_no_longer_exist(self):
        """Nodes churn pods continuously; without pruning this map grows for
        the lifetime of the DaemonSet.
        """
        core = FakeCoreForResolver({
            "web-1": FakeOwned("web-1", [FakeOwner("DaemonSet", "web")]),
            "web-2": FakeOwned("web-2", [FakeOwner("DaemonSet", "web")]),
        })
        resolver = WorkloadResolver(core, FakeAppsForResolver())
        resolver.resolve("shop", "web-1")
        resolver.resolve("shop", "web-2")

        resolver.prune({("shop", "web-1")})

        assert ("shop", "web-2") not in resolver._cache
        assert ("shop", "web-1") in resolver._cache


class RecordingMQ:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish(self, routing_key, payload):
        self.published.append((routing_key, payload))


class StubResolver:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.pruned_with = None

    def resolve(self, namespace, pod):
        return self.mapping.get(pod, pod)

    def prune(self, live_keys):
        self.pruned_with = live_keys


class TestCollectCycle:
    """The assembly step: what the collector actually puts on the bus."""

    def _run(self, summary, resolver=None):
        from svc_collector.main import _collect_once

        core = FakeCoreV1(summary)
        mq = RecordingMQ()
        _collect_once(core, mq, resolver or StubResolver())
        return mq.published

    def test_publishes_one_node_event_plus_one_per_pod(self):
        summary = _summary(pods=[
            {"podRef": {"name": "web", "namespace": "shop"}, "containers": []},
            {"podRef": {"name": "api", "namespace": "shop"}, "containers": []},
        ])
        published = self._run(summary)

        assert len(published) == 3
        assert all(key == "metric.raw" for key, _ in published)

    def test_the_node_event_has_no_pod(self):
        """cost-intelligence uses a null pod to recognise the node-level
        aggregate and skip it — it is not billable to any one workload.
        """
        node_event = self._run(_summary())[0][1]

        assert node_event["resource"]["pod"] is None
        assert node_event["resource"]["node"] == "test-node"

    def test_pod_events_carry_the_resolved_workload(self):
        """The publish-side half of bug 3: without `service` populated,
        cost-intelligence falls back to the pod name and identity churns on
        every restart.
        """
        summary = _summary(pods=[{"podRef": {"name": "web-7d9f-abcde", "namespace": "shop"}, "containers": []}])
        published = self._run(summary, StubResolver({"web-7d9f-abcde": "web"}))

        pod_event = published[1][1]
        assert pod_event["resource"]["pod"] == "web-7d9f-abcde", "pod detail is kept for drill-down"
        assert pod_event["resource"]["service"] == "web", "identity is the stable workload name"

    def test_pruning_covers_exactly_the_live_pods(self):
        summary = _summary(pods=[
            {"podRef": {"name": "web", "namespace": "shop"}, "containers": []},
            {"podRef": {"name": "api", "namespace": "other"}, "containers": []},
        ])
        resolver = StubResolver()
        from svc_collector.main import _collect_once

        _collect_once(FakeCoreV1(summary), RecordingMQ(), resolver)

        assert resolver.pruned_with == {("shop", "web"), ("other", "api")}

    def test_published_events_validate_against_the_schema(self):
        """Every downstream consumer rebuilds MetricEvent from these, so a
        shape change here breaks the whole pipeline silently.
        """
        from hypertrace_common.schemas import MetricEvent

        summary = _summary(pods=[{"podRef": {"name": "web", "namespace": "shop"}, "containers": []}])
        for _key, payload in self._run(summary):
            MetricEvent.model_validate(payload)


class TestLifecycleEventMapping:
    def test_maps_only_events_that_matter_for_cost(self):
        from svc_collector.k8s_events import _REASON_TO_TYPE

        assert _REASON_TO_TYPE["OOMKilling"].value == "pod_oom_killed"
        assert _REASON_TO_TYPE["ScalingReplicaSet"].value == "deployment_scaled"
        assert _REASON_TO_TYPE["SuccessfulRescale"].value == "hpa_scaled"

    def test_unrelated_reasons_are_not_mapped(self):
        """decision-policy treats these as "a deployment just happened", so
        mapping routine noise like image pulls would misclassify anomalies
        as deployment bugs.
        """
        from svc_collector.k8s_events import _REASON_TO_TYPE

        for reason in ("Pulled", "Scheduled", "FailedMount"):
            assert reason not in _REASON_TO_TYPE
