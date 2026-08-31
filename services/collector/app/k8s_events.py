"""Watches cluster-wide Kubernetes Events and turns the ones relevant to
cost/behaviour analysis into LifecycleEvent messages (FR-2: pod
create/delete/restart/OOM-kill, deployment scaling, HPA scaling events).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from kubernetes import client, watch

from hypertrace_common.schemas import LifecycleEvent, LifecycleEventType, ResourceRef

logger = logging.getLogger(__name__)

_REASON_TO_TYPE = {
    "Started": LifecycleEventType.POD_CREATED,
    "Killing": LifecycleEventType.POD_DELETED,
    "BackOff": LifecycleEventType.POD_RESTARTED,
    "OOMKilling": LifecycleEventType.POD_OOM_KILLED,
    "ScalingReplicaSet": LifecycleEventType.DEPLOYMENT_SCALED,
    "SuccessfulRescale": LifecycleEventType.HPA_SCALED,
}


def watch_lifecycle_events(
    core_v1: client.CoreV1Api,
    cluster: str,
    node_name: str,
    on_event: Callable[[LifecycleEvent], None],
) -> None:
    """Blocking watch loop over cluster-wide Events.

    The Kubernetes watch API times out server-side after a while by design;
    on any stream error (including that timeout) this just restarts the
    watch rather than treating it as fatal.
    """
    w = watch.Watch()
    while True:
        try:
            for item in w.stream(core_v1.list_event_for_all_namespaces, timeout_seconds=300):
                raw = item["object"]
                event_type = _REASON_TO_TYPE.get(raw.reason or "")
                if event_type is None:
                    continue

                involved = raw.involved_object
                timestamp = raw.last_timestamp or raw.event_time or raw.first_timestamp
                if timestamp is None:
                    continue

                on_event(
                    LifecycleEvent(
                        timestamp=timestamp,
                        resource=ResourceRef(
                            cluster=cluster,
                            namespace=involved.namespace or "default",
                            node=node_name,
                            pod=involved.name if involved.kind == "Pod" else None,
                        ),
                        event_type=event_type,
                        reason=raw.reason or "",
                        message=raw.message or "",
                    )
                )
        except Exception:
            logger.exception("Event watch stream failed, restarting")
