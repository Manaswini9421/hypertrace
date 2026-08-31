import os

# Blast-radius containment (doc Section 11.3): cap how many remediation
# actions this executor will perform in a rolling window, so a bug in
# HyperTrace itself can't cascade into mass changes across the cluster.
MAX_ACTIONS_PER_WINDOW = int(os.environ.get("MAX_ACTIONS_PER_WINDOW", "5"))
RATE_LIMIT_WINDOW_MINUTES = int(os.environ.get("RATE_LIMIT_WINDOW_MINUTES", "10"))

# Second, independent copy of the hard-coded protected-namespace floor from
# decision-policy. Deliberately duplicated rather than shared: the executor
# is the component actually holding cluster write credentials, so it must
# refuse a protected target even if a buggy or compromised decision-policy
# dispatches one (doc 11.3 — defense in depth, not DRY).
PROTECTED_NAMESPACE_PREFIXES = tuple(
    p.strip() for p in os.environ.get("PROTECTED_NAMESPACES", "kube-system").split(",") if p.strip()
)
