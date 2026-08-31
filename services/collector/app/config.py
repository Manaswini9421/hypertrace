"""Collector-specific runtime config, read from the DaemonSet's env vars
(see infra/k8s/services/collector-daemonset.yaml). Deliberately separate
from hypertrace_common.config, which only covers cross-service infra
connections (RabbitMQ/DB) — these fields are specific to this one agent.
"""

import os

NODE_NAME = os.environ["NODE_NAME"]  # injected via fieldRef: spec.nodeName
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "kind-hypertrace")
COLLECT_INTERVAL_SECONDS = float(os.environ.get("COLLECT_INTERVAL_SECONDS", "10"))
