from __future__ import annotations

import random

import pytest

from quiet.probe.calibrate import calibrate
from quiet.probe.model import ALL_SUBSTATS, SUBSTATS
from quiet.probe.score import MIN_RESOURCE_SAMPLES, compute_effects, score_pair, verdict
from tests import synth


def thresholds(n: int = 60, seed: int = 5):
    rng = random.Random(seed)
    return calibrate([synth.null_pair(rng) for _ in range(n)])


class TestEffects:
    def test_emits_every_substatistic(self):
        rng = random.Random(0)
        normal, fault = synth.null_pair(rng)
        effects = compute_effects(normal, fault)
        assert sorted(effects) == sorted(ALL_SUBSTATS)

    def test_rejects_schema_mismatch(self):
        rng = random.Random(0)
        normal, fault = synth.null_pair(rng)
        fault.schema_version = 999
        with pytest.raises(ValueError, match="schema mismatch"):
            compute_effects(normal, fault)

    def test_missing_source_degrades_only_its_own_channel(self):
        rng = random.Random(0)
        normal, fault = synth.dose_pair(rng, payment_error_rate=0.5)
        normal.traces = None
        fault.traces = None
        effects = compute_effects(normal, fault)
        assert not effects["metric.trace_error_z"].usable
        assert not effects["metric.latency_bm_z"].usable
        assert effects["log.error_rate_z"].usable, "log channel must survive"


class TestUnusableIsNotZero:
    def test_unusable_channel_leaves_the_denominator(self):
        """An absent measurement is not evidence of absence: it must not
        be scored 0 and drag explicitness down."""
        rng = random.Random(0)
        normal, fault = synth.dose_pair(rng, payment_error_rate=0.9)
        normal.logs = None
        fault.logs = None
        _, v = score_pair(normal, fault, thresholds())
        assert "log" in v.unusable_channels
        assert "log" not in v.per_channel
        assert v.n_usable_channels == 2
        assert v.explicitness_frac == pytest.approx(v.explicitness / 2)

    def test_all_unusable_yields_none_fraction(self):
        rng = random.Random(0)
        normal, fault = synth.null_pair(rng)
        for snap in (normal, fault):
            snap.events = snap.logs = snap.traces = snap.resources = None
        _, v = score_pair(normal, fault, thresholds())
        assert v.n_usable_channels == 0
        assert v.explicitness_frac is None

    def test_underpowered_resource_series_is_excluded(self):
        """At a 1m scrape interval a short window yields too few points.
        That degrades to unusable, not to a zero."""
        rng = random.Random(0)
        few = {"payment-0|cpu": (0.1, 0.001, MIN_RESOURCE_SAMPLES - 1)}
        normal = synth.snapshot(rng, label="normal", resource_series=few)
        fault = synth.snapshot(rng, label="fault", resource_series=few, offset_min=10)
        effects = compute_effects(normal, fault)
        assert not effects["metric.resource_t"].usable


class TestDoseResponse:
    """H1: explicitness rises monotonically with injected dose."""

    DOSES = [0.0, 0.10, 0.25, 0.50, 1.00]

    def _margins(self, seed: int) -> list[float]:
        thr = thresholds()
        rng = random.Random(seed)
        out = []
        for dose in self.DOSES:
            normal, fault = synth.dose_pair(rng, payment_error_rate=dose)
            effects = compute_effects(normal, fault)
            out.append(effects["metric.trace_error_z"].statistic)
        return out

    def test_trace_error_statistic_is_monotone_in_dose(self):
        stats = self._margins(seed=42)
        assert all(a < b for a, b in zip(stats, stats[1:])), stats

    def test_sham_scores_zero_and_full_dose_scores_nonzero(self):
        """PREREG checks (a) and the top of the curve, in one place."""
        thr = thresholds()
        rng = random.Random(7)

        sham_scores = [
            verdict(compute_effects(*synth.null_pair(rng)), thr).explicitness
            for _ in range(30)
        ]
        assert sum(sham_scores) / len(sham_scores) < 0.3

        full_scores = [
            verdict(
                compute_effects(*synth.dose_pair(rng, payment_error_rate=1.0)), thr
            ).explicitness
            for _ in range(10)
        ]
        assert all(s >= 1 for s in full_scores)

    def test_integer_score_saturates_but_margin_does_not(self):
        """Why PREREG regresses the margin, not the integer: the integer
        tops out and flattens the upper half of the curve."""
        thr = thresholds()
        rng = random.Random(3)
        high, higher = 0.5, 1.0
        v_high = verdict(
            compute_effects(*synth.dose_pair(rng, payment_error_rate=high)), thr
        )
        v_higher = verdict(
            compute_effects(*synth.dose_pair(rng, payment_error_rate=higher)), thr
        )
        assert v_high.explicitness == v_higher.explicitness, "integer saturates"
        assert (
            v_higher.margins["metric.trace_error_z"]
            > v_high.margins["metric.trace_error_z"]
        ), "margin still separates them"


class TestLocalizedFaults:
    def test_max_over_services_survives_dilution(self):
        """paymentFailure hits one service out of many. A namespace-wide
        aggregate would dilute it; the per-service maximum must not."""
        rng = random.Random(11)
        quiet_services = {f"svc{i}": (2000, 0.001, 20.0) for i in range(12)}

        normal = synth.snapshot(rng, label="normal", services=quiet_services)
        faulty = dict(quiet_services)
        faulty["svc0"] = (2000, 0.08, 20.0)
        fault = synth.snapshot(rng, label="fault", services=faulty, offset_min=10)

        effects = compute_effects(normal, fault)
        eff = effects["metric.trace_error_z"]
        assert eff.usable
        assert eff.statistic > 4.0, "localized 8% error rate must be visible"
        assert "svc0" in eff.note

        # The aggregate a naive implementation would have used.
        agg_error = sum(s.error_spans for s in fault.traces.per_service.values())
        agg_total = sum(s.spans for s in fault.traces.per_service.values())
        assert agg_error / agg_total < 0.01, "diluted to under 1% in aggregate"

    def test_records_entity_count_for_multiplicity_audit(self):
        """The max's null is only valid while the entity set is stable,
        so the count has to be in the artifact."""
        rng = random.Random(0)
        normal, fault = synth.dose_pair(rng, payment_error_rate=0.3)
        effects = compute_effects(normal, fault)
        assert effects["metric.trace_error_z"].detail["n_entities"] == 2


class TestVerdictShape:
    def test_fired_keys_identify_the_channel_that_spoke(self):
        thr = thresholds()
        rng = random.Random(9)
        normal, fault = synth.dose_pair(rng, payment_error_rate=1.0)
        v = verdict(compute_effects(normal, fault), thr)
        assert v.fired
        assert all(k in ALL_SUBSTATS for k in v.fired)
        assert v.per_channel["metric"] == 1

    def test_explicitness_never_exceeds_channel_count(self):
        thr = thresholds()
        rng = random.Random(1)
        for dose in (0.0, 0.1, 0.5, 1.0):
            v = verdict(
                compute_effects(*synth.dose_pair(rng, payment_error_rate=dose)), thr
            )
            assert 0 <= v.explicitness <= len(SUBSTATS)

    def test_disabled_threshold_cannot_fire(self):
        thr = thresholds()
        thr.tau["metric.trace_error_z"] = None
        thr.tau["metric.latency_bm_z"] = None
        thr.tau["metric.resource_t"] = None
        rng = random.Random(2)
        v = verdict(
            compute_effects(*synth.dose_pair(rng, payment_error_rate=1.0)), thr
        )
        assert v.per_channel["metric"] == 0
        assert "metric.trace_error_z" not in v.margins
