"""Tests for policy evaluation and the safety floors (doc 14.4, FR-7, NFR-4)."""

from svc_decision.policy import is_protected, policy_matches

PROTECTED = ("kube-system",)


class TestPolicyMatching:
    def test_empty_rule_matches_everything(self):
        assert policy_matches({}, "misconfiguration_or_waste", "ns/app", 1.0)

    def test_classification_filter_admits_and_rejects(self):
        rule = {"classifications": ["suspected_abuse"]}
        assert policy_matches(rule, "suspected_abuse", "ns/app", 1.0)
        assert not policy_matches(rule, "misconfiguration_or_waste", "ns/app", 1.0)

    def test_cost_threshold_is_inclusive(self):
        rule = {"min_cost_per_hour": 1.0}
        assert policy_matches(rule, "x", "ns/app", 1.0), "a rule at exactly the threshold should fire"
        assert not policy_matches(rule, "x", "ns/app", 0.99)

    def test_service_prefix_scopes_the_rule(self):
        rule = {"service_prefix": "hypertrace/"}
        assert policy_matches(rule, "x", "hypertrace/victim", 1.0)
        assert not policy_matches(rule, "x", "kube-system/kube-proxy", 1.0)

    def test_all_conditions_must_hold(self):
        rule = {"classifications": ["a"], "min_cost_per_hour": 0.5, "service_prefix": "ns/"}
        assert policy_matches(rule, "a", "ns/app", 1.0)
        assert not policy_matches(rule, "a", "ns/app", 0.1), "cost condition should veto the match"
        assert not policy_matches(rule, "b", "ns/app", 1.0), "classification condition should veto the match"


class TestProtectedFloor:
    """The hard-coded floor from doc 11.3 — no user policy may override it."""

    def test_system_namespace_is_protected(self):
        assert is_protected("kube-system/kube-proxy", PROTECTED)

    def test_application_namespace_is_not(self):
        assert not is_protected("hypertrace/victim", PROTECTED)

    def test_multiple_prefixes_are_honoured(self):
        assert is_protected("kube-public/thing", ("kube-system", "kube-public"))

    def test_similar_prefix_does_not_falsely_protect(self):
        """A namespace merely starting with the same letters is a different
        namespace; being over-protective would silently disable remediation.
        """
        assert not is_protected("hypertrace/kube-system-lookalike", PROTECTED)
