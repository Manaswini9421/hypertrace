"""Resolves a Pod to the stable name of the workload that owns it.

Why this exists: a pod's name contains a generated suffix that changes on
every restart, rollout, and rescale. Keying a service's identity on the pod
name means a workload's cost history and behavioural baseline are thrown
away every time it restarts — including restarts that HyperTrace itself
causes when it patches a Deployment to throttle it. The baseline that
detected an incident would be destroyed by the response to that incident,
and could never mature past a few samples.

So identity is the owning workload (`namespace/deployment`), which survives
restarts, and pod-level detail stays in ResourceRef.pod for drill-down.

Ownership is walked through ownerReferences (Pod -> ReplicaSet -> Deployment)
rather than parsed out of the pod name, because the name's suffix format is
not a stable API contract. Results are cached with a TTL since ownership
almost never changes and this runs every collection cycle on every node.
"""

from __future__ import annotations

import logging
import time

from kubernetes import client

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300


class WorkloadResolver:
    def __init__(self, core_v1: client.CoreV1Api, apps_v1: client.AppsV1Api) -> None:
        self._core_v1 = core_v1
        self._apps_v1 = apps_v1
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    def resolve(self, namespace: str, pod_name: str) -> str:
        """Returns the owning workload's name, falling back to the pod's own
        name for pods with no controller (bare pods, static control-plane
        pods) — those genuinely have no stabler identity.
        """
        key = (namespace, pod_name)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[1] < CACHE_TTL_SECONDS:
            return cached[0]

        workload = self._lookup(namespace, pod_name)
        self._cache[key] = (workload, now)
        return workload

    def _lookup(self, namespace: str, pod_name: str) -> str:
        try:
            pod = self._core_v1.read_namespaced_pod(pod_name, namespace)
            owner = next(iter(pod.metadata.owner_references or []), None)
            if owner is None:
                return pod_name

            if owner.kind == "ReplicaSet":
                rs = self._apps_v1.read_namespaced_replica_set(owner.name, namespace)
                rs_owner = next(iter(rs.metadata.owner_references or []), None)
                return rs_owner.name if rs_owner else owner.name

            # DaemonSet / StatefulSet / Job own their pods directly, so the
            # owner's name is already the stable workload name.
            return owner.name
        except Exception:
            logger.warning("could not resolve owner for %s/%s, using pod name", namespace, pod_name, exc_info=False)
            return pod_name

    def prune(self, live_keys: set[tuple[str, str]]) -> None:
        """Drops cache entries for pods that no longer exist, so a long-lived
        collector on a churning node doesn't grow this map without bound.
        """
        for key in set(self._cache) - live_keys:
            del self._cache[key]
