"""Policy evaluation (doc 14.4): priority-ordered rule matching against a
classification / service id / cost rate. `rule_dsl` is a small JSON dict,
not a full expression language — deliberately simple so a rule can be read
straight off the audit log by a human, matching doc 13's point that
explainability matters more than sophistication when you have to defend
"why did it flag this?" in front of a panel.

rule_dsl fields (all optional; an absent field matches anything):
  classifications: list[str]   only match these classification values
  min_cost_per_hour: float     only match if cost_per_hour >= this
  service_prefix: str          only match services whose id starts with this
  requires_approval: bool      if true, the matched action is recorded as
                                pending_approval instead of executed immediately
  min_confidence: float        only match if the detector's confidence in
                                this anomaly is at least this high (§25.1)
"""

from __future__ import annotations

from typing import Any


def policy_matches(
    rule_dsl: dict[str, Any],
    classification: str,
    service: str,
    cost_per_hour: float,
    confidence: float = 1.0,
) -> bool:
    min_confidence = rule_dsl.get("min_confidence")
    if min_confidence is not None and confidence < min_confidence:
        return False

    classifications = rule_dsl.get("classifications")
    if classifications and classification not in classifications:
        return False

    min_cost = rule_dsl.get("min_cost_per_hour")
    if min_cost is not None and cost_per_hour < min_cost:
        return False

    prefix = rule_dsl.get("service_prefix")
    if prefix and not service.startswith(prefix):
        return False

    return True


def is_protected(service: str, protected_prefixes: tuple[str, ...]) -> bool:
    return any(service.startswith(prefix) for prefix in protected_prefixes)
