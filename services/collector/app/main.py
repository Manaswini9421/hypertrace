"""Collector agent entrypoint.

Runs as a Kubernetes DaemonSet (one instance per node). Two concurrent
jobs, matching doc Section 3 Phase 1 "Observe":

  1. Every COLLECT_INTERVAL_SECONDS, pull this node's kubelet stats/summary
     (node-level + per-pod CPU/mem/net usage) and publish one MetricEvent
     per node/pod onto the shared RabbitMQ exchange.
  2. In a background thread, watch cluster-wide Kubernetes Events and
     publish the ones that matter (pod lifecycle, scaling) as
     LifecycleEvent messages.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from kubernetes import client, config as k8s_config

from hypertrace_common.messaging import (
    ROUTING_KEY_LIFECYCLE,
    ROUTING_KEY_METRIC,
    RabbitMQClient,
)
from hypertrace_common.schemas import LifecycleEvent, MetricEvent, ResourceRef

from . import config
from .k8s_events import watch_lifecycle_events
from .kubelet_client import fetch_stats_summary, parse_node_metrics, parse_pod_metrics
from .workload_resolver import WorkloadResolver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("collector")


def _publish_lifecycle_event(mq: RabbitMQClient, event: LifecycleEvent) -> None:
    mq.publish(ROUTING_KEY_LIFECYCLE, event.model_dump())


def _collect_once(core_v1: client.CoreV1Api, mq: RabbitMQClient, resolver: WorkloadResolver) -> None:
    summary = fetch_stats_summary(core_v1, config.NODE_NAME)
    now = datetime.now(timezone.utc)

    node_usage = parse_node_metrics(summary)
    mq.publish(
        ROUTING_KEY_METRIC,
        MetricEvent(
            timestamp=now,
            resource=ResourceRef(cluster=config.CLUSTER_NAME, namespace="", node=config.NODE_NAME),
            **node_usage,
        ).model_dump(),
    )

    pods = parse_pod_metrics(summary)
    live_keys: set[tuple[str, str]] = set()
    for pod_usage in pods:
        namespace = pod_usage.pop("namespace")
        pod = pod_usage.pop("pod")
        live_keys.add((namespace, pod))
        mq.publish(
            ROUTING_KEY_METRIC,
            MetricEvent(
                timestamp=now,
                resource=ResourceRef(
                    cluster=config.CLUSTER_NAME,
                    namespace=namespace,
                    pod=pod,
                    node=config.NODE_NAME,
                    service=resolver.resolve(namespace, pod),
                ),
                **pod_usage,
            ).model_dump(),
        )
    resolver.prune(live_keys)

    logger.info("published metrics for node=%s (%d pods)", config.NODE_NAME, len(pods))


def _collect_loop(core_v1: client.CoreV1Api, mq: RabbitMQClient, resolver: WorkloadResolver) -> None:
    while True:
        started = time.monotonic()
        try:
            _collect_once(core_v1, mq, resolver)
        except Exception:
            logger.exception("metric collection cycle failed")

        elapsed = time.monotonic() - started
        time.sleep(max(0.0, config.COLLECT_INTERVAL_SECONDS - elapsed))


def main() -> None:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    core_v1 = client.CoreV1Api()
    resolver = WorkloadResolver(core_v1, client.AppsV1Api())
    metrics_mq = RabbitMQClient()
    events_mq = RabbitMQClient()

    events_thread = threading.Thread(
        target=watch_lifecycle_events,
        args=(
            core_v1,
            config.CLUSTER_NAME,
            config.NODE_NAME,
            lambda event: _publish_lifecycle_event(events_mq, event),
        ),
        daemon=True,
        name="lifecycle-event-watcher",
    )
    events_thread.start()

    logger.info(
        "collector starting: node=%s cluster=%s interval=%ss",
        config.NODE_NAME,
        config.CLUSTER_NAME,
        config.COLLECT_INTERVAL_SECONDS,
    )
    _collect_loop(core_v1, metrics_mq, resolver)


if __name__ == "__main__":
    main()
