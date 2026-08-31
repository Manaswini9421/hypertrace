"""Loads the pricing model from a mounted ConfigMap
(infra/k8s/services/cost-intelligence.yaml) and turns raw resource usage
into a $/hour figure — doc Section 3 Phase 2 "Understand".
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

_BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class PricingModel:
    cpu_core_hour: float
    memory_gb_hour: float

    def cost_per_hour(self, cpu_cores: float, memory_bytes: int) -> float:
        memory_gb = memory_bytes / _BYTES_PER_GB
        return cpu_cores * self.cpu_core_hour + memory_gb * self.memory_gb_hour


def load_pricing(path: str) -> PricingModel:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return PricingModel(
        cpu_core_hour=float(raw["cpu_core_hour"]),
        memory_gb_hour=float(raw["memory_gb_hour"]),
    )
