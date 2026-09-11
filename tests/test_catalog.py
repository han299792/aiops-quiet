"""The catalogue is the deliverable, so it gets an end-to-end test.

Synthetic runs are written to a temp directory in the same layout the
cluster produces, then scored. No cluster, no network.
"""

from __future__ import annotations

import random

import pytest

from quiet.analysis.catalog import (
    fit_thresholds,
    noop_fire_rate,
    load_runs,
    render,
    sanity,
    score_all,
)
from quiet.harness.rundir import RunDir, run_id
from tests import synth


def write_run(root, pid: str, normal, fault, replicate: int = 1) -> None:
    rd = RunDir.create(root, run_id(pid, replicate))
    rd.write("normal.json", normal)
    rd.write("fault.json", fault)
    rd.finalize({"problem_id": pid, "status": "ok"})


def build_catalog(tmp_path, *, noop_runs: int = 20, loud_score: float = 1.0, seed: int = 0):
    """noop arms + one loud fault + one quiet fault."""
    rng = random.Random(seed)
    root = tmp_path / "runs"
    root.mkdir()

    for r in range(1, noop_runs + 1):
        n, f = synth.null_pair(rng)
        for snap in (n, f):
            snap.spec = snap.spec.model_copy(
                update={"problem_id": "noop_detection_hotel_reservation-1"}
            )
        write_run(root, "noop_detection_hotel_reservation-1", n, f, replicate=r)

    # Loud: big error rate AND restarts AND warning events.
    n, f = synth.dose_pair(rng, payment_error_rate=loud_score)
    f.events = f.events.model_copy(
        update={"warning_count": 40, "restart_delta": 3, "pod_churn": 2}
    )
    f.logs = f.logs.model_copy(update={"per_pod": {"payment-0": (900, 1000)}})
    for snap in (n, f):
        snap.spec = snap.spec.model_copy(update={"problem_id": "pod_kill_hotel_res-detection-1"})
    write_run(root, "pod_kill_hotel_res-detection-1", n, f)

    # Quiet: nothing moves at all.
    n, f = synth.null_pair(rng)
    for snap in (n, f):
        snap.spec = snap.spec.model_copy(
            update={"problem_id": "astronomy_shop_image_slow_load-detection-1"}
        )
    write_run(root, "astronomy_shop_image_slow_load-detection-1", n, f)
    return root


class TestLoading:
    def test_loads_completed_runs(self, tmp_path):
        root = build_catalog(tmp_path)
        assert len(load_runs(root)) == 22

    def test_skips_runs_without_the_completion_marker(self, tmp_path):
        root = build_catalog(tmp_path)
        rng = random.Random(1)
        n, f = synth.null_pair(rng)
        rd = RunDir.create(root, run_id("crashed", 1))
        rd.write("normal.json", n)
        rd.write("fault.json", f)  # no run.json
        assert len(load_runs(root)) == 22, "an unfinished run must not be counted"

    def test_ignores_archaeology(self, tmp_path):
        root = build_catalog(tmp_path)
        (root / "_archaeology").mkdir()
        assert len(load_runs(root)) == 22


class TestCalibration:
    def test_refuses_without_enough_noop_runs(self, tmp_path):
        root = build_catalog(tmp_path, noop_runs=1)
        with pytest.raises(SystemExit, match="noop"):
            fit_thresholds(load_runs(root))

    def test_thresholds_come_from_noop_only(self, tmp_path):
        """A threshold derived from the faulty runs would be circular."""
        runs = load_runs(build_catalog(tmp_path))
        thr, _ = fit_thresholds(runs)
        assert all(v > 0 for v in thr.n_null.values() if v), "null sample is empty"
        # 20 noop runs, split in half, both orderings => <= 20
        assert max(thr.n_null.values()) <= 20


class TestSanityChecks:
    def test_passes_when_noop_is_zero_and_loud_is_three(self, tmp_path):
        runs = load_runs(build_catalog(tmp_path))
        thr, _ = fit_thresholds(runs)
        rows = score_all(runs, thr)
        fired, total = noop_fire_rate(rows)
        assert fired / total <= 0.34, f"noop fired {fired}/{total}"
        assert sanity(rows) == [], sanity(rows)

    def test_flags_a_meter_that_misses_a_loud_fault(self, tmp_path):
        """If pod_kill does not light up, nothing quieter can be trusted."""
        runs = load_runs(build_catalog(tmp_path))
        thr, _ = fit_thresholds(runs)
        rows = score_all(runs, thr)
        for r in rows:
            if "pod_kill" in r["problem_id"]:
                r["explicitness"] = 1
        issues = sanity(rows)
        assert any("loud fault" in i for i in issues)

    def test_flags_a_meter_that_fires_on_nothing(self, tmp_path):
        runs = load_runs(build_catalog(tmp_path))
        thr, _ = fit_thresholds(runs)
        rows = score_all(runs, thr)
        for r in rows:
            if "noop" in r["problem_id"]:
                r["explicitness"] = 2
        issues = sanity(rows)
        assert any("noop" in i for i in issues)

    def test_tolerates_the_odd_noop_firing(self, tmp_path):
        """Thresholds are the (1-alpha) quantile of the null, so a share of
        genuinely null runs exceeds them by construction. Demanding zero
        would contradict the calibration and push thresholds up until
        nothing ever fires."""
        runs = load_runs(build_catalog(tmp_path))
        thr, _ = fit_thresholds(runs)
        rows = score_all(runs, thr)
        noop = [r for r in rows if "noop" in r["problem_id"]]
        noop[0]["explicitness"] = 1          # one of six fires
        assert not any("noop fired" in i for i in sanity(rows))

    def test_too_few_noop_runs_show_up_as_an_unstable_meter(self, tmp_path):
        """Six null observations cannot pin a MAD-based threshold: half the
        genuinely null runs then exceed it. The check has to surface that
        rather than let an unstable meter score a catalogue."""
        runs = load_runs(build_catalog(tmp_path, noop_runs=6, seed=0))
        thr, _ = fit_thresholds(runs)
        rows = score_all(runs, thr)
        fired, total = noop_fire_rate(rows)
        assert fired / total > 0.34
        assert any("noop fired" in i for i in sanity(rows))

    def test_flags_a_missing_loud_arm(self, tmp_path):
        runs = [r for r in load_runs(build_catalog(tmp_path)) if "pod_kill" not in r[0]]
        thr, _ = fit_thresholds(runs)
        assert any("loud arm" in i for i in sanity(score_all(runs, thr)))


class TestTable:
    def test_ranks_loud_above_quiet(self, tmp_path):
        runs = load_runs(build_catalog(tmp_path))
        thr, _ = fit_thresholds(runs)
        rows = score_all(runs, thr)
        loud = next(r for r in rows if "pod_kill" in r["problem_id"])
        quiet = next(r for r in rows if "image_slow_load" in r["problem_id"])
        assert loud["explicitness"] > quiet["explicitness"]

    def test_renders_and_names_the_quiet_ones(self, tmp_path):
        runs = load_runs(build_catalog(tmp_path))
        thr, realized = fit_thresholds(runs)
        text = render(score_all(runs, thr), thr, realized)
        assert "명시성 카탈로그" in text
        assert "pod_kill_hotel_res-detection-1" in text
        assert "조용한 장애" in text
        assert "image_slow_load" in text

    def test_unusable_channel_shows_as_dash_not_zero(self, tmp_path):
        runs = load_runs(build_catalog(tmp_path))
        for _, n, f in runs:
            n.logs = None
            f.logs = None
        thr, realized = fit_thresholds(runs)
        rows = score_all(runs, thr)
        assert all(r["n_usable"] == 2 for r in rows)
        assert "-" in render(rows, thr, realized)
