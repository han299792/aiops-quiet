"""Calibration must deliver the false-positive rate it advertises.

The central claim of the whole measurement is that a genuinely null pair
scores explicitness 0. These tests check that claim directly, on data
where the truth is known by construction.
"""

from __future__ import annotations

import random

import pytest

from quiet.probe.calibrate import (
    calibrate,
    evaluate_null,
    expand_orderings,
    min_n_for_quantile,
    null_statistics,
    split_half,
)
from quiet.probe.model import ALL_SUBSTATS, SUBSTATS
from quiet.probe.score import compute_effects, verdict
from quiet.probe.stats import z_quantile
from tests import synth


def make_null_pairs(n: int, seed: int = 0):
    rng = random.Random(seed)
    return [synth.null_pair(rng) for _ in range(n)]


class TestCorrection:
    def test_family_wise_denominator_bounds_alpha(self):
        """Sum of per-key alpha shares must not exceed alpha overall."""
        thr = calibrate(make_null_pairs(30), alpha=0.05)
        # Within each channel the shares sum to alpha/k_channels; across
        # the three channels that totals alpha.
        total = sum(thr.alpha / thr.correction[k] for k in ALL_SUBSTATS)
        assert total == pytest.approx(0.05)

    def test_min_n_is_honest_about_being_unaffordable(self):
        # 9-way correction at alpha=.05 needs 360 null observations.
        assert min_n_for_quantile(9, 0.05) == 360
        assert min_n_for_quantile(3, 0.05) == 120

    def test_realistic_campaign_uses_mad_fallback(self):
        """A 30-run sham campaign cannot support empirical quantiles.

        This is recorded rather than hidden: a reviewer will ask which
        path was taken, and the answer must be in the artifact.
        """
        thr = calibrate(make_null_pairs(30), alpha=0.05)
        assert all(
            thr.method[k] == "mad_fallback" for k in ALL_SUBSTATS if thr.n_null[k]
        )


class TestBothOrderings:
    def test_expansion_doubles_the_sample(self):
        pairs = make_null_pairs(5)
        assert len(expand_orderings(pairs)) == 10

    def test_difference_statistics_symmetrize(self):
        """Plain differences negate under swap, so both orderings cancel."""
        nulls = null_statistics(make_null_pairs(40))
        for key in ("event.restart_delta", "event.pod_churn"):
            assert sum(nulls[key]) == pytest.approx(0.0, abs=1e-9)

    def test_max_statistics_do_not_symmetrize(self):
        """Guards the corrected reasoning: max over entities is NOT
        antisymmetric (swapping gives -min, not -max), so its null is
        legitimately right-shifted. A previous version of this suite
        asserted symmetry here and was simply wrong about the maths."""
        nulls = null_statistics(make_null_pairs(40))
        for key in ("log.error_rate_z", "metric.trace_error_z"):
            sample = nulls[key]
            assert len(sample) >= 40
            assert sum(sample) / len(sample) > 0.0, (
                "null of a maximum sits above zero"
            )


class TestFalsePositiveRate:
    def test_held_out_null_rarely_shows_explicitness(self):
        """Fit on one half, measure on the other. This is the check that
        PREREG calls (c), and the number it produces is the sham arm's
        measured explicitness error."""
        pairs = make_null_pairs(120, seed=7)
        fit, held = split_half(pairs, seed=1)
        thr = calibrate(fit, alpha=0.05)
        rates = evaluate_null(thr, held)
        assert rates["__any_channel__"] <= 0.15, (
            "a genuinely null pair should almost never score explicit; "
            f"got {rates['__any_channel__']:.3f}"
        )

    def test_normal_null_hits_the_corrected_quantile(self):
        """On exactly-normal data the MAD fallback should land on the
        nominal per-key rate, not somewhere arbitrary."""
        rng = random.Random(3)
        sample = [rng.gauss(0.0, 1.0) for _ in range(4000)]
        from quiet.probe.stats import mad, median

        alpha, m = 0.05, 9
        tau = median(sample) + z_quantile(1.0 - alpha / m) * mad(sample)
        exceed = sum(1 for v in sample if v > tau) / len(sample)
        assert exceed == pytest.approx(alpha / m, abs=0.004)

    def test_sham_pairs_score_zero_explicitness(self):
        """PREREG check (a): sham must read 0."""
        pairs = make_null_pairs(80, seed=11)
        thr = calibrate(pairs, alpha=0.05)
        rng = random.Random(999)
        scores = []
        for _ in range(40):
            normal, fault = synth.null_pair(rng)
            scores.append(verdict(compute_effects(normal, fault), thr).explicitness)
        mean_score = sum(scores) / len(scores)
        assert mean_score < 0.3, f"sham should read ~0 explicitness, got {mean_score}"


class TestDegenerateNulls:
    def test_never_measurable_key_is_disabled_not_permissive(self):
        """A statistic never usable in the null has no threshold at all."""
        pairs = make_null_pairs(10)
        # Strip traces so the trace-based keys are never usable.
        for a, b in pairs:
            a.traces = None
            b.traces = None
        thr = calibrate(pairs)
        assert thr.tau["metric.trace_error_z"] is None
        assert thr.n_null["metric.trace_error_z"] == 0

    def test_disabled_threshold_survives_json(self):
        """The reason tau is Optional rather than +inf: JSON has no
        infinity, so an inf sentinel round-trips to null and then either
        crashes or compares wrong."""
        from quiet.probe.model import Thresholds

        pairs = make_null_pairs(10)
        for a, b in pairs:
            a.traces = None
            b.traces = None
        thr = calibrate(pairs)
        again = Thresholds.model_validate_json(thr.model_dump_json())
        assert again.tau["metric.trace_error_z"] is None

    def test_constant_null_fires_on_first_deviation(self):
        """Restart counts are 0 throughout a healthy null. One restart
        must then count as explicit."""
        pairs = make_null_pairs(30)
        thr = calibrate(pairs)
        tau = thr.tau["event.restart_delta"]
        assert tau > 0.0, "a threshold of exactly 0 would fire on ties"
        assert 1.0 > tau, "a single real restart must exceed it"


class TestThresholdsArtifact:
    def test_records_everything_a_reviewer_will_ask_for(self):
        thr = calibrate(make_null_pairs(30), alpha=0.05)
        for key in ALL_SUBSTATS:
            assert key in thr.tau
            assert key in thr.method
            assert key in thr.correction
            assert key in thr.n_null
        assert thr.fitted_at.tzinfo is not None, "timestamp must be tz-aware"

    def test_round_trips_through_json(self):
        from quiet.probe.model import Thresholds

        thr = calibrate(make_null_pairs(25))
        again = Thresholds.model_validate_json(thr.model_dump_json())
        assert again.tau == thr.tau
        assert again.method == thr.method

    def test_channels_are_covered_exactly_once(self):
        seen = [k for keys in SUBSTATS.values() for k in keys]
        assert sorted(seen) == sorted(ALL_SUBSTATS)
        assert len(seen) == len(set(seen))
