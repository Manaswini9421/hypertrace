"""Confidence scoring (dossier §24.3).

Confidence is what gates authority, so it has to be a defined function
rather than a feeling: ≥0.85 permits autonomous action if policy also
allows it, ≥0.60 recommends and requires human approval, and below that the
system may only alert.

Two properties are deliberate and worth preserving under any change:

  * Every term is capped at 1.0, so no single enormous Z-score can push
    confidence to autonomous levels on its own. A CPU spike of forty
    standard deviations with no decoupling and no corroboration reaches
    0.40 and stays advisory.
  * Baseline maturity is a *term*, not a gate, so an immature baseline
    lowers confidence smoothly rather than switching detection off. That
    keeps behaviour continuous while a service warms up.
"""

from __future__ import annotations

from dataclasses import dataclass

# Corroboration is a three-valued input, not a free float: nothing, a
# soft signal (a recent deployment), or a hard one (a security rule).
CORROBORATION_NONE = 0.0
CORROBORATION_SOFT = 0.5
CORROBORATION_HARD = 1.0

# Samples before a baseline counts as mature. 720 = one sample per 10s for
# two hours, which is when the mean has stopped moving materially.
MATURITY_SAMPLES = 720

AUTONOMOUS_THRESHOLD = 0.85
APPROVAL_THRESHOLD = 0.60


@dataclass(frozen=True)
class Scores:
    cost_z: float
    cpu_z: float
    traffic_z: float

    @property
    def peak(self) -> float:
        """The strongest resource/cost signal — traffic is deliberately
        excluded, because high traffic is the explanation for a spike, not
        evidence of one.
        """
        return max(self.cost_z, self.cpu_z)


def confidence(scores: Scores, corroboration: float, n_samples: int) -> float:
    peak = scores.peak
    magnitude = _clamp((peak - 3.0) / 5.0)      # 3 sigma -> 0, 8 sigma -> 1
    decoupling = _clamp((peak - scores.traffic_z) / 6.0)
    maturity = _clamp(n_samples / MATURITY_SAMPLES)

    return round(
        0.40 * magnitude + 0.30 * decoupling + 0.20 * _clamp(corroboration) + 0.10 * maturity,
        3,
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def authority_for(score: float) -> str:
    """Maps a confidence score to how much authority it earns.

    Returns one of "autonomous", "approval" or "alert_only". The decision
    engine may narrow this further via policy, but never widen it.
    """
    if score >= AUTONOMOUS_THRESHOLD:
        return "autonomous"
    if score >= APPROVAL_THRESHOLD:
        return "approval"
    return "alert_only"
