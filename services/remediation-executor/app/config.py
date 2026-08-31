import os

# Blast-radius containment (dossier §17.2). Two independent ceilings: a
# global one so a bug upstream cannot cascade across the cluster, and a
# per-service one so a single noisy workload cannot consume the whole
# global budget and starve remediation everywhere else.
MAX_ACTIONS_PER_WINDOW = int(os.environ.get("MAX_ACTIONS_PER_WINDOW", "10"))
RATE_LIMIT_WINDOW_MINUTES = int(os.environ.get("RATE_LIMIT_WINDOW_MINUTES", "5"))
MAX_ACTIONS_PER_SERVICE = int(os.environ.get("MAX_ACTIONS_PER_SERVICE", "1"))
SERVICE_RATE_LIMIT_WINDOW_MINUTES = int(os.environ.get("SERVICE_RATE_LIMIT_WINDOW_MINUTES", "15"))

# How long after execution a rollback may still be applied (§17.2). Beyond
# this the recorded prior state is no longer a safe thing to restore — the
# workload may legitimately have been changed since, and reapplying a stale
# snapshot would silently revert someone else's work.
ROLLBACK_WINDOW_MINUTES = int(os.environ.get("ROLLBACK_WINDOW_MINUTES", "60"))

# Second, independent copy of the hard-coded protected-namespace floor from
# decision-policy. Deliberately duplicated rather than shared: the executor
# is the component actually holding cluster write credentials, so it must
# refuse a protected target even if a buggy or compromised decision-policy
# dispatches one (§17.1 — defense in depth, not DRY).
PROTECTED_NAMESPACE_PREFIXES = tuple(
    p.strip() for p in os.environ.get("PROTECTED_NAMESPACES", "kube-system").split(",") if p.strip()
)
