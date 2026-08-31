"""Reads node & pod resource usage from the kubelet's /stats/summary
endpoint, proxied through the Kubernetes API server so the collector only
needs normal in-cluster API access — no direct kubelet TLS/auth to manage,
and it works uniformly whether the collector is running in kind or a real
cloud-managed cluster (doc Section 3, Phase 1 "Observe").
"""

from __future__ import annotations

import json
from typing import Any

from kubernetes import client


def fetch_stats_summary(core_v1: client.CoreV1Api, node_name: str) -> dict[str, Any]:
    # _preload_content=False is required: with content preloading on, the
    # generated client "helpfully" deserializes the JSON response into a
    # dict and then, because this endpoint declares its return type as str,
    # stringifies that dict with Python's repr (single-quoted) instead of
    # returning the original JSON text — which json.loads then can't parse.
    # Raw mode gives us the actual response bytes to decode ourselves.
    response = core_v1.connect_get_node_proxy_with_path(
        node_name, "stats/summary", _preload_content=False
    )
    return json.loads(response.data)


def parse_node_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    node = summary.get("node", {})
    cpu = node.get("cpu", {})
    memory = node.get("memory", {})
    network = node.get("network", {})
    return {
        "cpu_usage_cores": (cpu.get("usageNanoCores") or 0) / 1e9,
        "memory_working_set_bytes": memory.get("workingSetBytes") or 0,
        "memory_rss_bytes": memory.get("rssBytes"),
        "network_rx_bytes_total": network.get("rxBytes"),
        "network_tx_bytes_total": network.get("txBytes"),
    }


def parse_pod_metrics(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns one usage dict per pod (namespace/pod plus the same usage
    fields as parse_node_metrics), aggregated across the pod's containers.
    """
    results = []
    for pod in summary.get("pods", []):
        ref = pod.get("podRef", {})
        if not ref.get("name"):
            continue

        cpu_cores = 0.0
        mem_bytes = 0
        for container in pod.get("containers", []):
            cpu_cores += (container.get("cpu", {}).get("usageNanoCores") or 0) / 1e9
            mem_bytes += container.get("memory", {}).get("workingSetBytes") or 0

        network = pod.get("network", {})
        results.append(
            {
                "namespace": ref.get("namespace"),
                "pod": ref.get("name"),
                "cpu_usage_cores": cpu_cores,
                "memory_working_set_bytes": mem_bytes,
                "network_rx_bytes_total": network.get("rxBytes"),
                "network_tx_bytes_total": network.get("txBytes"),
            }
        )
    return results
