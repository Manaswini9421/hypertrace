"""Tests for the Behaviour Analysis Engine's detector (doc 14.2, FR-5/FR-6)."""

import statistics

import pytest

from svc_behaviour.stats import BucketStats


class TestBucketStats:
    """Welford's online algorithm must match batch statistics exactly —
    the detector's credibility rests on the numbers being right.
    """

    def test_matches_batch_statistics(self):
        data = [1.0, 1.1, 0.9, 1.05, 0.95, 1.02, 0.98]
        stats = BucketStats()
        for value in data:
            stats.update(value)
        assert stats.mean == pytest.approx(statistics.mean(data))
        assert stats.stddev == pytest.approx(statistics.stdev(data))

    def test_single_sample_has_no_spread(self):
        stats = BucketStats()
        stats.update(1.0)
        assert stats.stddev == 0.0

    def test_zero_stddev_never_divides_by_zero(self):
        """A perfectly flat metric must score 0, not raise or return inf."""
        stats = BucketStats()
        for _ in range(10):
            stats.update(0.5)
        assert stats.z_score(99.0) == 0.0

    def test_flags_a_clear_outlier(self):
        stats = BucketStats()
        for value in [1.0, 1.1, 0.9, 1.05, 0.95, 1.02, 0.98]:
            stats.update(value)
        assert stats.z_score(5.0) > 3

    def test_survives_a_round_trip_through_storage(self):
        """Baselines persist to JSONB between messages, so serialisation must
        preserve the running state exactly.
        """
        original = BucketStats()
        for value in [1.0, 2.0, 3.0]:
            original.update(value)
        restored = BucketStats.from_dict(original.to_dict())
        assert (restored.n, restored.mean, restored.stddev) == (original.n, original.mean, original.stddev)

    def test_restores_from_empty_dict(self):
        stats = BucketStats.from_dict({})
        assert stats.n == 0


class TestBaselineSuppression:
    """Regression tests for the baseline-poisoning bug found during live
    testing: folding anomalous readings into the baseline made the detector
    progressively blind to repeated incidents.
    """

    def test_normal_reading_trains_the_baseline(self):
        from svc_behaviour.main import _should_learn

        assert _should_learn(should_flag=False, consecutive_flags=0) == (True, 0)

    def test_normal_reading_resets_the_counter(self):
        from svc_behaviour.main import _should_learn

        assert _should_learn(should_flag=False, consecutive_flags=9) == (True, 0)

    def test_flagged_reading_is_withheld_from_the_baseline(self):
        from svc_behaviour.main import _should_learn

        learn, consecutive = _should_learn(should_flag=True, consecutive_flags=0)
        assert learn is False
        assert consecutive == 1

    def test_sustained_shift_is_eventually_accepted(self):
        """Otherwise a workload that legitimately grew would alert forever."""
        from svc_behaviour.main import MAX_CONSECUTIVE_SUPPRESSED, _should_learn

        assert _should_learn(True, MAX_CONSECUTIVE_SUPPRESSED) == (True, 0)

    def test_repeated_incidents_stay_detectable(self):
        """The actual regression: three identical incidents, and the third
        must still be caught. Without suppression the third scored z<3 and
        went undetected.
        """
        def run(suppress: bool) -> list[float]:
            stats = BucketStats()
            for _ in range(20):
                stats.update(0.000065)  # idle baseline, with the tiny jitter real metrics have
                stats.update(0.000067)
            scores = []
            for _ in range(3):
                for _ in range(4):
                    z = stats.z_score(0.032)
                    scores.append(z)
                    if not (suppress and abs(z) > 3):
                        stats.update(0.032)
                for _ in range(6):
                    z = stats.z_score(0.000065)
                    if not (suppress and abs(z) > 3):
                        stats.update(0.000065)
            return scores

        assert run(suppress=False)[-1] < 3, "expected the unsuppressed detector to go blind"
        assert run(suppress=True)[-1] > 3, "suppressed detector must still catch the third incident"
