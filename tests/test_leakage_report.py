"""The analysis module must not overclaim.

These tests are mostly about what the report REFUSES to say: PREREG 7.4
makes no-verdict the default, 4.1 forbids dropping leaky runs, and 7.1
requires discards to appear as a number rather than as silence.
"""

from __future__ import annotations

import json

import pytest

from quiet.analysis.leakage import Comparison, Rate, Run, load, order_trend, report
from quiet.harness.rundir import RunDir


def make_run(root, rid, *, arm="observe", problem="astronomy_shop_payment_service_failure-detection-1",
             correct=True, leaked=False, censored=False, submit=5, cost=0.02,
             status="ok", grade=None):
    rd = RunDir.create(root, rid)
    rd.write("leak.json", {"leaked": leaked, "intent_only": False,
                           "leak_censored": censored, "submit_step": submit, "turns": 4,
                           "first_leak_step": 3 if leaked else None, "hits": []})
    rd.write("usage.json", {"cost_usd": cost})
    rd.write("session.json", {"results": {"Detection Accuracy":
                                          grade or ("Correct" if correct else "Incorrect")}})
    rd.finalize({"run_id": rid, "problem_id": problem, "arm": arm,
                 "replicate": 1, "status": status, "error": None})
    return rd


class TestLoad:
    def test_reads_a_finished_run(self, tmp_path):
        make_run(tmp_path, "r1", leaked=True, censored=True)
        runs, discards = load(tmp_path)
        assert len(runs) == 1 and discards == []
        assert runs[0].leaked and runs[0].correct and not runs[0].clean

    def test_skips_failed_and_unfinished_runs(self, tmp_path):
        make_run(tmp_path, "ok1")
        make_run(tmp_path, "bad", status="error")
        RunDir.create(tmp_path, "half").write("leak.json", {})  # no run.json
        runs, _ = load(tmp_path)
        assert [r.run_id for r in runs] == ["ok1"]

    def test_discards_are_read(self, tmp_path):
        make_run(tmp_path, "r1")
        (tmp_path / "discarded.jsonl").write_text(
            json.dumps({"run_id": "d1", "phase": "fault", "problems": ["x"]}) + "\n")
        _, discards = load(tmp_path)
        assert discards[0]["phase"] == "fault"

    def test_a_leak_after_the_submission_leaves_the_run_clean(self, tmp_path):
        """PREREG 4.1: it cannot have informed an answer already given."""
        make_run(tmp_path, "r1", leaked=True, censored=False)
        runs, _ = load(tmp_path)
        assert runs[0].leaked and runs[0].clean


class TestNoVerdictIsTheDefault:
    def test_overlapping_intervals_give_no_verdict(self):
        c = Comparison("x", Rate(5, 10), Rate(4, 10))
        assert c.verdict.startswith("no verdict")
        assert "trend" not in c.verdict

    def test_a_separated_difference_is_reported(self):
        c = Comparison("x", Rate(30, 30), Rate(0, 30))
        assert c.verdict.startswith("observe is higher")

    def test_no_data_is_not_a_verdict(self):
        assert Comparison("x", Rate(0, 0), Rate(0, 0)).verdict == "no data"

    def test_a_zero_block_arm_does_not_break_the_interval(self):
        """0/n is the outcome the block arm is designed to produce, and the
        normal approximation misbehaves exactly there."""
        c = Comparison("x", Rate(9, 12), Rate(0, 12))
        assert c.z is not None
        assert -1.0 <= c.diff_lo <= c.diff_hi <= 1.0


class TestIndependenceCheck:
    def test_reports_when_it_cannot_check(self):
        assert "too few" in order_trend([])

    def test_stable_accuracy_reports_no_drift(self, tmp_path):
        for i in range(8):
            make_run(tmp_path, f"r{i}", correct=(i % 2 == 0))
        runs, _ = load(tmp_path)
        assert "no drift" in order_trend(runs)

    def test_a_collapse_is_flagged(self, tmp_path):
        for i in range(16):
            make_run(tmp_path, f"r{i:02d}", correct=(i < 8))
        runs, _ = load(tmp_path)
        assert order_trend(runs).startswith("★ DRIFT")


class TestReport:
    def test_empty_directory_says_so(self, tmp_path):
        assert "Nothing to report" in report(tmp_path)

    def test_single_arm_does_not_invent_a_contrast(self, tmp_path):
        make_run(tmp_path, "r1")
        text = report(tmp_path)
        assert "No contrast to report" in text
        assert "H1 leak rate" not in text

    def test_both_arms_produce_the_contrast(self, tmp_path):
        for i in range(6):
            make_run(tmp_path, f"o{i}", arm="observe", leaked=True, censored=True)
            make_run(tmp_path, f"b{i}", arm="block", leaked=False)
        text = report(tmp_path)
        assert "H1 leak rate" in text and "H2 accuracy" in text

    def test_discard_rate_is_stated_even_when_zero(self, tmp_path):
        make_run(tmp_path, "r1")
        assert "0/1 runs discarded" in report(tmp_path)

    def test_missing_null_arm_is_called_out(self, tmp_path):
        """Silently reporting uncorrected detection rates is the failure
        PREREG 7.2 exists to prevent."""
        make_run(tmp_path, "r1")
        assert "uncorrected" in report(tmp_path)

    def test_leaky_runs_stay_in_the_denominator(self, tmp_path):
        """PREREG 4.1. Both columns are the result; neither replaces the other."""
        make_run(tmp_path, "clean", leaked=False, correct=True)
        make_run(tmp_path, "leaky", leaked=True, censored=True, correct=True)
        text = report(tmp_path)
        assert "2/2" in text          # correct over all runs
        assert "1/1" in text          # correct over clean runs only


class TestBlockEfficacy:
    """PREREG 3.2/8. The block is a command-string filter and is bypassable,
    so whether it worked is a measurement, not an assumption."""

    def test_a_leaky_block_suspends_the_H2_verdict(self, tmp_path):
        for i in range(8):
            make_run(tmp_path, f"o{i}", arm="observe", leaked=True, censored=True)
            make_run(tmp_path, f"b{i}", arm="block", leaked=True, censored=True)
        text = report(tmp_path)
        assert "The block leaked" in text
        assert "not judged" in text

    def test_a_held_block_leaves_H2_interpretable(self, tmp_path):
        for i in range(8):
            make_run(tmp_path, f"o{i}", arm="observe", leaked=True, censored=True)
            make_run(tmp_path, f"b{i}", arm="block", leaked=False)
        text = report(tmp_path)
        assert "The block held" in text
        assert "The block leaked" not in text

    def test_efficacy_is_reported_before_the_hypotheses(self, tmp_path):
        for i in range(4):
            make_run(tmp_path, f"o{i}", arm="observe")
            make_run(tmp_path, f"b{i}", arm="block")
        text = report(tmp_path)
        assert text.index("block efficacy") < text.index("H2 accuracy")


class TestInvalidFormat:
    """An unparseable submission is not the same failure as a wrong answer,
    and folding them together inflates whatever the arm is blamed for."""

    def test_it_is_counted_as_not_correct(self, tmp_path):
        make_run(tmp_path, "r1", grade="Invalid Format")
        runs, _ = load(tmp_path)
        assert runs[0].correct is False and runs[0].grade == "Invalid Format"

    def test_it_is_called_out_separately(self, tmp_path):
        make_run(tmp_path, "r1", grade="Invalid Format", arm="block")
        make_run(tmp_path, "r2", correct=True, arm="block")
        text = report(tmp_path)
        assert "Invalid Format" in text and "block 1" in text

    def test_silence_when_there_are_none(self, tmp_path):
        make_run(tmp_path, "r1")
        assert "Invalid Format" not in report(tmp_path)
