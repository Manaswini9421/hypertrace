import os

# Hard-coded, non-overridable policy floor (doc Section 11.3: "a hard-coded
# policy tier that sits below the user-configurable policy layer"). Any
# service whose id ("namespace/pod") starts with one of these namespace
# prefixes is NEVER auto-remediated, no matter what policy matches —
# protects the cluster's own control-plane/system workloads by default.
PROTECTED_NAMESPACE_PREFIXES = tuple(
    p.strip() for p in os.environ.get("PROTECTED_NAMESPACES", "kube-system").split(",") if p.strip()
)

# How recent a lifecycle event has to be to count as "a deployment just
# happened" for the joint classification (doc 14.3).
RECENT_DEPLOYMENT_WINDOW_MINUTES = int(os.environ.get("RECENT_DEPLOYMENT_WINDOW_MINUTES", "15"))

# How recently a workload must have tripped a runtime-security rule for a
# cost anomaly to be read as corroborated abuse rather than plain waste
# (doc 14.3's "egress_to_unusual_geo or process_signature_matches_miner").
SECURITY_SIGNAL_WINDOW_MINUTES = int(os.environ.get("SECURITY_SIGNAL_WINDOW_MINUTES", "10"))
