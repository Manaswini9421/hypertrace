"""Tests for confidence scoring and the authority it earns (dossier §24.3).

Confidence is what gates autonomous action, so these are the tests that
decide whether the system may change production infrastructure without
asking. Both properties the spec calls deliberate are asserted here: every
term is capped, and maturity lowers confidence smoothly rather than
switching detection off.
"""

import pytest

from svc_behaviour.confidence import (
    APPROVAL_THRESHOLD,
    AUTONOMOUS_THRESHOLD,
    CORROBORATION_HARD,
    CORROBORATION_NONE,
    CORROBORATION_SOFT,
    MATURITY_SAMPLES,
    Scores,
    authority_for,
    confidence,
)


def mature_scores(cost_z=8.0, cpu_z=8.0, traffic_z=0.0) -> Scores:
    return Scores(cost_z=cost_z, cpu_z=cpu_z, traffic_z=traffic_z)


class TestConfidence:
    def test_a_strong_corroborated_mature_signal_earns_autonomy(self):
        score = confidence(mature_scores(), CORROBORATION_HARD, MATURITY_SAMPLES)
        assert score >= AUTONOMOUS_THRESHOLD

    def test_an_enormous_single_signal_stays_advisory(self):
        """The property §24.3 calls out explicitly: a 40-sigma spike with no
        decoupling and no corroboration must not reach autonomous levels on
        magnitude alone. Capping every term is what prevents that.
        """
        score = confidence(
            Scores(cost_z=40.0, cpu_z=40.0, traffic_z=40.0), CORROBORATION_NONE, MATURITY_SAMPLES
        )
        assert score < AUTONOMOUS_THRESHOLD
        assert authority_for(score) != "autonomous"

    def test_traffic_moving_with_cost_destroys_the_decoupling_term(self):
        decoupled = confidence(mature_scores(traffic_z=0.0), CORROBORATION_NONE, MATURITY_SAMPLES)
        coupled = confidence(mature_scores(traffic_z=8.0), CORROBORATION_NONE, MATURITY_SAMPLES)
        assert coupled < decoupled

    def test_corroboration_raises_confidence_monotonically(self):
        base = mature_scores()
        none = confidence(base, CORROBORATION_NONE, MATURITY_SAMPLES)
        soft = confidence(base, CORROBORATION_SOFT, MATURITY_SAMPLES)
        hard = confidence(base, CORROBORATION_HARD, MATURITY_SAMPLES)
        assert none < soft < hard

    def test_an_immature_baseline_lowers_confidence_without_silencing_it(self):
        """Maturity is a term, not a gate — a young baseline should still be
        able to raise an alert, just not to act on its own.
        """
        young = confidence(mature_scores(), CORROBORATION_HARD, n_samples=10)
        old = confidence(mature_scores(), CORROBORATION_HARD, n_samples=MATURITY_SAMPLES)
        assert 0 < young < old

    def test_every_score_stays_within_bounds(self):
        extremes = [
            confidence(Scores(1e6, 1e6, -1e6), 10.0, 10**9),
            confidence(Scores(0.0, 0.0, 1e6), -5.0, 0),
            confidence(Scores(3.0, 3.0, 3.0), CORROBORATION_NONE, 0),
        ]
        assert all(0.0 <= s <= 1.0 for s in extremes), extremes

    def test_traffic_above_the_peak_does_not_produce_a_negative_term(self):
        """Traffic outrunning cost is legitimate growth, not negative
        evidence — the term floors at zero rather than subtracting.
        """
        assert confidence(Scores(4.0, 4.0, 100.0), CORROBORATION_NONE, MATURITY_SAMPLES) >= 0.0

    def test_peak_ignores_traffic(self):
        """Traffic must never raise the peak: high traffic is the
        explanation for a spike, not evidence of one.
        """
        assert Scores(cost_z=4.0, cpu_z=2.0, traffic_z=99.0).peak == 4.0


class TestAuthority:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (1.00, "autonomous"),
            (AUTONOMOUS_THRESHOLD, "autonomous"),
            (AUTONOMOUS_THRESHOLD - 0.01, "approval"),
            (APPROVAL_THRESHOLD, "approval"),
            (APPROVAL_THRESHOLD - 0.01, "alert_only"),
            (0.0, "alert_only"),
        ],
    )
    def test_thresholds_are_inclusive_at_the_boundary(self, score, expected):
        assert authority_for(score) == expected


class TestPolicyConfidenceGate:
    def test_a_policy_can_demand_a_minimum_confidence(self):
        from svc_decision.policy import policy_matches

        rule = {"min_confidence": 0.85}
        assert policy_matches(rule, "suspected_abuse", "ns/app", 1.0, confidence=0.9)
        assert not policy_matches(rule, "suspected_abuse", "ns/app", 1.0, confidence=0.5)

    def test_absent_min_confidence_matches_anything(self):
        from svc_decision.policy import policy_matches

        assert policy_matches({}, "suspected_abuse", "ns/app", 1.0, confidence=0.0)


class TestDetectionCondition:
    """The conjunction from §24.2 — the thing that separates this from a
    single-metric cost alert.
    """

    def _evaluate(self, **kwargs):
        from svc_behaviour.main import evaluate_condition

        defaults = {"cost_z": 8.0, "cpu_z": 8.0, "traffic_z": 0.0, "mature": True}
        return evaluate_condition(**{**defaults, **kwargs})

    def test_cost_up_traffic_flat_qualifies(self):
        qualifies, explains = self._evaluate(traffic_z=0.5)
        assert qualifies and not explains

    def test_cost_up_traffic_up_is_explained_not_flagged(self):
        """The flash-sale case. Acting here would be worse than the problem
        it is trying to solve (dossier §11.1).
        """
        qualifies, explains = self._evaluate(traffic_z=6.0)
        assert not qualifies
        assert explains

    def test_traffic_merely_elevated_still_qualifies(self):
        """The decoupling test needs traffic to be *unremarkable*, not low —
        a service serving somewhat more than usual is still decoupled if its
        cost moved by three sigma (§24.2).
        """
        qualifies, _ = self._evaluate(traffic_z=0.9)
        assert qualifies

    def test_traffic_between_the_bands_neither_qualifies_nor_explains(self):
        """Between the 1.0 ceiling and the 3.0 co-movement floor the signal
        is ambiguous, and the design errs toward doing nothing (§24.5).
        """
        qualifies, explains = self._evaluate(traffic_z=2.0)
        assert not qualifies and not explains

    def test_cost_must_move_not_just_cpu(self):
        """Cost is the signal this system exists to act on; CPU alone is
        what every other monitoring tool already alerts on.
        """
        qualifies, _ = self._evaluate(cost_z=1.0, cpu_z=9.0)
        assert not qualifies

    def test_an_immature_baseline_never_qualifies(self):
        qualifies, explains = self._evaluate(mature=False)
        assert not qualifies and not explains

    def test_absent_traffic_is_not_treated_as_zero_traffic(self):
        """Reading a missing signal as zero would make every service look
        decoupled and flag the entire cluster.
        """
        qualifies, explains = self._evaluate(traffic_z=None)
        assert not qualifies and not explains


class TestClassificationPrecedence:
    def test_the_detectors_traffic_verdict_is_final(self):
        """Cause is already settled when traffic explains the movement;
        re-deriving it would discard the one signal that settles it.
        """
        from svc_decision.main import _classify

        classification, reason = _classify("ns/app", "legitimate_traffic_growth")
        assert classification.value == "legitimate_traffic_growth"
        assert reason == {"traffic_explains": True}

    def test_an_unclassified_verdict_is_resolved_locally(self):
        from svc_decision.main import _classify

        classification, _ = _classify("ns/app", "unclassified")
        assert classification.value == "misconfiguration_or_waste"
