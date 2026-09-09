from __future__ import annotations

from dataclasses import replace

import pytest

from quiet.analysis.power import (
    METRIC_CORRECTION,
    Scenario,
    analytic_threshold,
    detection_rate,
    min_detectable_dose,
    spans_needed_for,
    sweep,
)


class TestScenario:
    def test_dose_is_diluted_by_charge_fraction(self):
        """A 10% dose is not a 10% service error rate: paymentFailure
        only fails charge requests."""
        s = Scenario(spans_per_service=1000, baseline_error_rate=0.0, charge_fraction=0.25)
        assert s.fault_error_rate(0.10) == pytest.approx(0.025)
        assert s.fault_error_rate(1.00) == pytest.approx(0.25)

    def test_rate_is_clamped(self):
        s = Scenario(spans_per_service=100, baseline_error_rate=0.9, charge_fraction=1.0)
        assert s.fault_error_rate(1.0) == 1.0


class TestThreshold:
    def test_more_services_raises_the_bar(self):
        s = Scenario(spans_per_service=1000)
        assert analytic_threshold(s, n_entities=50) > analytic_threshold(s, n_entities=2)

    def test_matches_the_pipeline_correction(self):
        s = Scenario(spans_per_service=1000, n_services=1)
        from quiet.probe.stats import z_quantile

        assert analytic_threshold(s) == pytest.approx(
            z_quantile(1.0 - (s.alpha / METRIC_CORRECTION))
        )


class TestDetectionRate:
    def test_zero_dose_is_the_false_positive_rate(self):
        s = Scenario(spans_per_service=2000)
        assert detection_rate(s, 0.0, trials=400, seed=0) <= 0.05

    def test_monotone_in_dose(self):
        s = Scenario(spans_per_service=500, charge_fraction=0.05)
        rates = [detection_rate(s, d, trials=300, seed=1) for d in (0.0, 0.25, 0.5, 1.0)]
        assert all(a <= b for a, b in zip(rates, rates[1:])), rates

    def test_monotone_in_span_volume(self):
        base = Scenario(spans_per_service=0, charge_fraction=0.05)
        rates = [
            detection_rate(replace(base, spans_per_service=n), 0.25, trials=300, seed=2)
            for n in (200, 500, 1000, 2000)
        ]
        assert rates[-1] > rates[0]

    def test_high_baseline_error_buries_a_low_dose(self):
        """If the app is already erroring at 5%, a 10% dose is invisible.
        Stage B has to measure this before the ladder is trusted."""
        quiet_app = Scenario(spans_per_service=1000, baseline_error_rate=0.002)
        noisy_app = Scenario(spans_per_service=1000, baseline_error_rate=0.05)
        assert detection_rate(quiet_app, 0.10, trials=300, seed=3) > 0.8
        assert detection_rate(noisy_app, 0.10, trials=300, seed=3) < 0.5


class TestPlanningHelpers:
    def test_spans_needed_grows_as_dose_shrinks(self):
        t = Scenario(spans_per_service=0, charge_fraction=0.05)
        need_low = spans_needed_for(0.25, template=t, trials=300, seed=4)
        need_high = spans_needed_for(1.00, template=t, trials=300, seed=4)
        assert need_low is not None and need_high is not None
        assert need_low > need_high

    def test_returns_none_when_undetectable_in_range(self):
        hopeless = Scenario(spans_per_service=0, charge_fraction=0.001)
        assert (
            spans_needed_for(0.10, template=hopeless, candidates=(100, 200), trials=100)
            is None
        )

    def test_min_detectable_dose_finds_the_knee(self):
        s = Scenario(spans_per_service=1000, charge_fraction=0.05)
        assert min_detectable_dose(s, trials=300, seed=5) in (0.25, 0.50, 0.75, 1.00)

    def test_sweep_covers_the_grid(self):
        t = Scenario(spans_per_service=0)
        table = sweep(t, span_counts=(200, 500), doses=(0.0, 0.5), trials=50)
        assert set(table) == {200, 500}
        assert set(table[200]) == {0.0, 0.5}
