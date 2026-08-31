"""Pydantic models for the message/DB entities defined in the project dossier
(docs/report.html, Section 5.2 "Data model"). These are the shapes every
service agrees on when publishing to or consuming from RabbitMQ, and the
shapes persisted into TimescaleDB/PostgreSQL from Phase 2 onward.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ResourceRef(BaseModel):
    """Identifies the Kubernetes object a metric/event/cost figure belongs to."""

    cluster: str
    namespace: str
    node: str
    pod: str | None = None
    container: str | None = None
    service: str | None = None  # logical service name, derived from labels


class MetricEvent(BaseModel):
    """One sample of raw resource utilisation (doc 5.2: raw_metrics; FR-1)."""

    timestamp: datetime
    resource: ResourceRef
    cpu_usage_cores: float
    memory_working_set_bytes: int
    memory_rss_bytes: int | None = None
    network_rx_bytes_total: int | None = None
    network_tx_bytes_total: int | None = None
    disk_read_bytes_total: int | None = None
    disk_write_bytes_total: int | None = None


class LifecycleEventType(str, enum.Enum):
    POD_CREATED = "pod_created"
    POD_DELETED = "pod_deleted"
    POD_RESTARTED = "pod_restarted"
    POD_OOM_KILLED = "pod_oom_killed"
    DEPLOYMENT_SCALED = "deployment_scaled"
    HPA_SCALED = "hpa_scaled"


class LifecycleEvent(BaseModel):
    """Kubernetes control-plane event relevant to cost/behaviour analysis (FR-2)."""

    timestamp: datetime
    resource: ResourceRef
    event_type: LifecycleEventType
    reason: str
    message: str


class CostEvent(BaseModel):
    """doc 5.2: cost_events (FR-3/FR-4).

    `service` is the cost_events.service identifier (currently
    "namespace/pod" — see cost-intelligence's _service_id) — distinct from
    `resource.service`, which is the logical/label-derived service name the
    collector doesn't populate yet.
    """

    timestamp: datetime
    service: str
    resource: ResourceRef
    resource_type: str  # e.g. "cpu", "memory", "network_egress"
    unit_rate: float
    cost_per_hour: float
    cost_per_unit_of_work: float | None = None
    # Carried alongside the cost so the detector can score the primary
    # resource signal (§24.1) without subscribing to raw metrics too.
    cpu_cores: float | None = None


class Baseline(BaseModel):
    """doc 5.2: baselines — rolling per-service behavioural profile (FR-5)."""

    service: str
    metric: str
    rolling_mean: float
    rolling_stddev: float
    day_of_week_profile: dict[str, float] = Field(default_factory=dict)
    updated_at: datetime


class TrafficSample(BaseModel):
    """Request rate for one workload — the business signal, and the
    denominator of the decoupling test (dossier §24.1).

    Scored so its Z-score can be compared against the resource and cost
    Z-scores, never so that a high value alone triggers anything: heavy
    traffic is not an incident, it is the explanation for one.
    """

    timestamp: datetime
    service: str
    requests_per_second: float


class SecuritySignal(BaseModel):
    """A runtime-security observation about a workload, used to corroborate
    a cost anomaly into a `suspected_abuse` classification (doc 14.3).

    This is the integration contract for an eBPF-based runtime security tool
    (Falco/Tetragon — doc Section 7.4). Any producer that emits this shape
    onto `security.signal` feeds the Decision Engine's joint reasoning; see
    services/security-signal-adapter for the current emitter and what is
    and isn't real about it.
    """

    timestamp: datetime
    service: str
    rule: str  # e.g. "unexpected_outbound_connection"
    severity: str  # "info" | "warning" | "critical"
    detail: dict[str, Any] = Field(default_factory=dict)


class AnomalyClassification(str, enum.Enum):
    LEGITIMATE_TRAFFIC_GROWTH = "legitimate_traffic_growth"
    LIKELY_BUG_FROM_DEPLOYMENT = "likely_bug_from_deployment"
    MISCONFIGURATION_OR_WASTE = "misconfiguration_or_waste"
    SUSPECTED_ABUSE = "suspected_abuse"
    UNCLASSIFIED = "unclassified"


class Anomaly(BaseModel):
    """doc 5.2: anomalies (FR-6)."""

    id: str
    service: str
    score: float
    classification: AnomalyClassification = AnomalyClassification.UNCLASSIFIED
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: str = "open"
    created_at: datetime


class RemediationAction(str, enum.Enum):
    ALERT_ONLY = "alert_only"
    THROTTLE = "throttle"
    FREEZE_SCALING = "freeze_scaling"
    QUARANTINE = "quarantine"
    TERMINATE = "terminate"


class Policy(BaseModel):
    """doc 5.2: policies (FR-7/FR-10)."""

    org_id: str
    rule_dsl: dict[str, Any]
    scope: str  # tag or namespace expression
    action: RemediationAction
    priority: int = 0


class ActionLogEntry(BaseModel):
    """doc 5.2: actions_log — append-only audit record (FR-9/FR-11)."""

    id: str
    anomaly_id: str
    action_type: RemediationAction
    executed_at: datetime
    result: str
    rollback_ref: str | None = None
