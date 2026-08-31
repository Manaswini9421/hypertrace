"""Kubernetes actions the Remediation Executor can perform (doc Section 3
Phase 4, FR-8). Only throttle and freeze_scaling are implemented —
quarantine/terminate are deferred as higher-blast-radius per doc 11.7's
scope-realism guidance.

A service id here is `namespace/workload` (e.g. "hypertrace/victim"), where
the workload name is the stable owner resolved by the collector — see
services/collector/app/workload_resolver.py.

KNOWN LIMITATION — throttle restarts the workload. doc Section 3's action
table describes throttle as capping CPU "without killing it", but patching
a Deployment's container resource limits necessarily triggers a rolling
restart, so the pods ARE replaced. The cost is capped as intended and the
workload stays available through the rollout, but this is more disruptive
than the dossier claims, and in-flight requests are dropped. A genuinely
non-disruptive implementation needs the Kubernetes in-place pod resize
feature (`resize` subresource, alpha in 1.31 and behind a feature gate),
which is why it isn't used here. State this plainly rather than repeating
the dossier's "without killing it" wording.
"""

from __future__ import annotations

import json
from typing import Any

from kubernetes import client

THROTTLE_CPU_LIMIT = "100m"  # conservative cap applied by the throttle action


def _split_service(service: str) -> tuple[str, str]:
    namespace, _, workload = service.partition("/")
    return namespace, workload


def throttle(apps_v1: client.AppsV1Api, core_v1: client.CoreV1Api, service: str) -> dict[str, Any]:
    del core_v1  # kept for signature symmetry with the other actions
    namespace, deployment_name = _split_service(service)
    try:
        deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
    except client.ApiException as exc:
        if exc.status == 404:
            return {"status": "no_op", "reason": f"no Deployment named {deployment_name}", "rollback_ref": None}
        raise
    container = deployment.spec.template.spec.containers[0]
    previous_limit = (container.resources.limits or {}).get("cpu") if container.resources else None

    if previous_limit == THROTTLE_CPU_LIMIT:
        # Idempotency (doc 5.4): a retried/duplicate throttle for an
        # already-throttled workload is a no-op, not a second "capped" event
        # that would clobber the real rollback reference with the throttled
        # value.
        return {"status": "no_op", "reason": "already throttled", "rollback_ref": None}

    patch = {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": container.name, "resources": {"limits": {"cpu": THROTTLE_CPU_LIMIT}}}]}
            }
        }
    }
    apps_v1.patch_namespaced_deployment(deployment_name, namespace, patch)

    rollback_ref = json.dumps(
        {
            "kind": "deployment_cpu_limit",
            "namespace": namespace,
            "deployment": deployment_name,
            "container": container.name,
            "previous_cpu_limit": previous_limit,
        }
    )
    return {
        "status": "executed",
        "reason": f"capped {deployment_name}/{container.name} cpu limit to {THROTTLE_CPU_LIMIT}",
        "rollback_ref": rollback_ref,
    }


def freeze_scaling(
    autoscaling_v2: client.AutoscalingV2Api,
    apps_v1: client.AppsV1Api,
    core_v1: client.CoreV1Api,
    service: str,
) -> dict[str, Any]:
    del apps_v1, core_v1  # kept for signature symmetry with the other actions
    namespace, deployment_name = _split_service(service)

    hpas = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace)
    hpa = next((h for h in hpas.items if h.spec.scale_target_ref.name == deployment_name), None)
    if hpa is None:
        return {"status": "no_op", "reason": f"no HPA targets {deployment_name}", "rollback_ref": None}

    previous_max = hpa.spec.max_replicas
    current_replicas = hpa.status.current_replicas

    # `is None` rather than a falsy check: 0 is a legitimate replica count,
    # and `current_replicas or previous_max` silently treated a
    # scaled-to-zero workload as "status unavailable".
    if current_replicas is None:
        return {"status": "no_op", "reason": "HPA has not reported a replica count yet", "rollback_ref": None}

    if current_replicas == 0:
        # Nothing is running, so nothing is scaling or costing anything.
        # Pinning maxReplicas at 1 here would also block a legitimate
        # scale-up later, which is worse than doing nothing.
        return {"status": "no_op", "reason": "workload is scaled to zero", "rollback_ref": None}

    if previous_max == current_replicas:
        return {"status": "no_op", "reason": "already frozen at current replica count", "rollback_ref": None}

    patch = {"spec": {"maxReplicas": current_replicas}}
    autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(hpa.metadata.name, namespace, patch)

    rollback_ref = json.dumps(
        {
            "kind": "hpa_max_replicas",
            "namespace": namespace,
            "hpa": hpa.metadata.name,
            "previous_max_replicas": previous_max,
        }
    )
    return {
        "status": "executed",
        "reason": f"froze {hpa.metadata.name} maxReplicas at {current_replicas}",
        "rollback_ref": rollback_ref,
    }


def rollback(apps_v1: client.AppsV1Api, autoscaling_v2: client.AutoscalingV2Api, rollback_ref_json: str) -> dict[str, Any]:
    ref = json.loads(rollback_ref_json)
    if ref["kind"] == "deployment_cpu_limit":
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": ref["container"], "resources": {"limits": {"cpu": ref["previous_cpu_limit"]}}}
                        ]
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(ref["deployment"], ref["namespace"], patch)
        return {
            "status": "rolled_back",
            "reason": f"restored {ref['deployment']}/{ref['container']} cpu limit to {ref['previous_cpu_limit']}",
        }
    if ref["kind"] == "hpa_max_replicas":
        patch = {"spec": {"maxReplicas": ref["previous_max_replicas"]}}
        autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(ref["hpa"], ref["namespace"], patch)
        return {
            "status": "rolled_back",
            "reason": f"restored {ref['hpa']} maxReplicas to {ref['previous_max_replicas']}",
        }
    return {"status": "failed", "reason": f"unknown rollback kind: {ref['kind']}"}
