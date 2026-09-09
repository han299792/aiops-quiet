"""Statistics are checked against hand-computable values, not snapshots."""

from __future__ import annotations

import math

import pytest

from quiet.probe.model import DUR_BIN_EDGES_MS, hist_quantile, make_hist
from quiet.probe.stats import (
    brunner_munzel_z_from_hist,
    mad,
    quantile,
    rate_ratio_z,
    two_proportion_z,
    welch_t,
    wilson_interval,
)


class TestTwoProportionZ:
    def test_identical_rates_give_zero(self):
        assert two_proportion_z(50, 1000, 50, 1000) == pytest.approx(0.0)

    def test_sign_follows_fault_window(self):
        assert two_proportion_z(100, 1000, 50, 1000) > 0
        assert two_proportion_z(50, 1000, 100, 1000) < 0

    def test_antisymmetric_under_swap(self):
        a = two_proportion_z(80, 1000, 20, 1000)
        b = two_proportion_z(20, 1000, 80, 1000)
        assert a == pytest.approx(-b)

    def test_known_value(self):
        # p_f=.10, p_n=.05, pooled=.075, se=sqrt(.075*.925*(2/1000))
        z = two_proportion_z(100, 1000, 50, 1000)
        se = math.sqrt(0.075 * 0.925 * (2 / 1000))
        assert z == pytest.approx(0.05 / se)

    def test_larger_sample_gives_larger_z_for_same_effect(self):
        small = two_proportion_z(10, 100, 5, 100)
        large = two_proportion_z(100, 1000, 50, 1000)
        assert large > small

    def test_undefined_cases_return_none(self):
        assert two_proportion_z(0, 0, 0, 0) is None
        assert two_proportion_z(0, 100, 0, 100) is None, "pooled rate 0 has no variance"
        assert two_proportion_z(100, 100, 100, 100) is None, "pooled rate 1 has no variance"


class TestRateRatioZ:
    def test_equal_rates_give_zero(self):
        assert rate_ratio_z(10, 5.0, 10, 5.0) == pytest.approx(0.0)

    def test_normalizes_for_unequal_windows(self):
        # 20 events in 10 min vs 10 in 5 min is the same rate.
        assert rate_ratio_z(20, 10.0, 10, 5.0) == pytest.approx(0.0)

    def test_antisymmetric_under_swap(self):
        a = rate_ratio_z(30, 10.0, 5, 10.0)
        b = rate_ratio_z(5, 10.0, 30, 10.0)
        assert a == pytest.approx(-b)

    def test_no_events_anywhere_is_undefined(self):
        assert rate_ratio_z(0, 10.0, 0, 10.0) is None


class TestWelchT:
    def test_equal_means_give_zero(self):
        assert welch_t(1.0, 0.1, 10, 1.0, 0.1, 10) == pytest.approx(0.0)

    def test_needs_two_samples_per_side(self):
        assert welch_t(1.0, 0.1, 1, 0.0, 0.1, 10) is None

    def test_zero_variance_is_undefined(self):
        assert welch_t(1.0, 0.0, 10, 0.0, 0.0, 10) is None

    def test_known_value(self):
        t = welch_t(2.0, 1.0, 10, 1.0, 1.0, 10)
        assert t == pytest.approx(1.0 / math.sqrt(0.2))


class TestBrunnerMunzel:
    def test_identical_distributions_give_zero(self):
        h = make_hist([1.0] * 50 + [10.0] * 50)
        assert brunner_munzel_z_from_hist(h, h) == pytest.approx(0.0)

    def test_slower_fault_window_is_positive(self):
        fast = make_hist([1.0] * 200)
        slow = make_hist([100.0] * 200)
        assert brunner_munzel_z_from_hist(slow, fast) > 0
        assert brunner_munzel_z_from_hist(fast, slow) < 0

    def test_antisymmetric_under_swap(self):
        a_h = make_hist([1.0, 2.0, 3.0, 50.0] * 25)
        b_h = make_hist([1.0, 2.0, 3.0, 4.0] * 25)
        a = brunner_munzel_z_from_hist(a_h, b_h)
        b = brunner_munzel_z_from_hist(b_h, a_h)
        assert a == pytest.approx(-b)

    def test_empty_is_undefined(self):
        empty = make_hist([])
        full = make_hist([1.0] * 10)
        assert brunner_munzel_z_from_hist(empty, full) is None

    def test_mismatched_binning_rejected(self):
        with pytest.raises(ValueError):
            brunner_munzel_z_from_hist([0, 0], [0, 0, 0])

    def test_insensitive_to_extreme_tail(self):
        """A handful of enormous outliers must not dominate, which is the
        whole reason for using a rank statistic over a mean shift."""
        base = [10.0] * 200
        with_tail = [10.0] * 199 + [60000.0]
        z = brunner_munzel_z_from_hist(make_hist(with_tail), make_hist(base))
        assert abs(z) < 1.0


class TestHistQuantile:
    def test_empty_returns_none(self):
        assert hist_quantile(make_hist([]), 0.5) is None

    def test_median_of_tight_distribution_is_close(self):
        h = make_hist([100.0] * 1000)
        q = hist_quantile(h, 0.5)
        # Bins are ~6 per decade, so within a factor of 10**(1/6) ~= 1.47.
        assert 100.0 / 1.5 <= q <= 100.0 * 1.5

    def test_is_monotone_in_q(self):
        h = make_hist([1.0, 5.0, 20.0, 100.0, 900.0] * 40)
        qs = [hist_quantile(h, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)]
        assert all(a <= b for a, b in zip(qs, qs[1:]))

    def test_rejects_out_of_range_q(self):
        with pytest.raises(ValueError):
            hist_quantile(make_hist([1.0]), 1.5)

    def test_overflow_lands_in_top_bin(self):
        h = make_hist([1e9])
        assert hist_quantile(h, 0.5) == pytest.approx(DUR_BIN_EDGES_MS[-1])


class TestQuantileAndMad:
    def test_type7_matches_numpy_defaults(self):
        s = [1.0, 2.0, 3.0, 4.0]
        assert quantile(s, 0.0) == 1.0
        assert quantile(s, 1.0) == 4.0
        assert quantile(s, 0.5) == pytest.approx(2.5)
        assert quantile(s, 0.25) == pytest.approx(1.75)

    def test_mad_of_constant_is_zero(self):
        assert mad([7.0] * 10) == 0.0

    def test_mad_approximates_sigma_for_normal_data(self):
        import random

        rng = random.Random(1)
        sample = [rng.gauss(0.0, 2.0) for _ in range(20000)]
        assert mad(sample) == pytest.approx(2.0, rel=0.05)


class TestWilson:
    def test_five_of_ten_is_wide(self):
        lo, hi = wilson_interval(5, 10)
        # The interval PREREG quotes as the reason N=10 is exploratory.
        assert lo == pytest.approx(0.24, abs=0.02)
        assert hi == pytest.approx(0.76, abs=0.02)

    def test_stays_inside_unit_interval_at_extremes(self):
        assert wilson_interval(0, 10)[0] == 0.0
        assert wilson_interval(10, 10)[1] == 1.0

    def test_narrows_with_n(self):
        w10 = wilson_interval(5, 10)
        w100 = wilson_interval(50, 100)
        assert (w100[1] - w100[0]) < (w10[1] - w10[0])

    def test_rejects_impossible_counts(self):
        with pytest.raises(ValueError):
            wilson_interval(11, 10)
