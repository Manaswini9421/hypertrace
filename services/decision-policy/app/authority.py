"""How much authority a confidence score earns (dossier §24.3).

Kept as its own module, and deliberately duplicated from the behaviour
engine's copy rather than imported across a service boundary: the decision
engine is the component that acts on this, so it must not depend on the
detector shipping a compatible version to stay safe.
"""

from __future__ import annotations

AUTONOMOUS_THRESHOLD = 0.85
APPROVAL_THRESHOLD = 0.60


def authority_for(score: float) -> str:
    """Returns "autonomous", "approval" or "alert_only"."""
    if score >= AUTONOMOUS_THRESHOLD:
        return "autonomous"
    if score >= APPROVAL_THRESHOLD:
        return "approval"
    return "alert_only"
