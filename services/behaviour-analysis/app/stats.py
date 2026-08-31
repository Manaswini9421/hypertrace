"""Bucketed rolling Z-score baseline (doc 14.2, "the core, explainable
detector"). Welford's online algorithm keeps a numerically stable running
mean/variance per (service, hour-of-week bucket), updated one sample at a
time as CostEvents arrive — no need to hold a history window in memory,
and it survives restarts via the `baselines` table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BucketStats:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0  # sum of squared deviations from the running mean

    def update(self, value: float) -> None:
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        delta2 = value - self.mean
        self.m2 += delta * delta2

    @property
    def stddev(self) -> float:
        if self.n < 2:
            return 0.0
        return (self.m2 / (self.n - 1)) ** 0.5

    def z_score(self, value: float) -> float:
        stddev = self.stddev
        if stddev == 0:
            return 0.0
        return (value - self.mean) / stddev

    def to_dict(self) -> dict[str, float]:
        return {"n": self.n, "mean": self.mean, "m2": self.m2}

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> "BucketStats":
        return cls(n=int(data.get("n", 0)), mean=data.get("mean", 0.0), m2=data.get("m2", 0.0))
