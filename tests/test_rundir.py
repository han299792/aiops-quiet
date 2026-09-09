from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from quiet.harness.rundir import (
    MARKER,
    RunDir,
    RunDirError,
    incomplete_runs,
    iter_runs,
    run_id,
    timestamp_slug,
)
from quiet.probe.model import WindowSpec


class TestRunId:
    def test_sorts_chronologically(self):
        early = run_id("a", 1, when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        late = run_id("a", 1, when=datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert early < late

    def test_carries_condition_and_replicate(self):
        rid = run_id("payment_dose_10", 3)
        assert "payment_dose_10" in rid and rid.endswith("r03")

    def test_slug_is_filesystem_safe(self):
        slug = timestamp_slug(datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc))
        assert ":" not in slug
        assert slug == "2026-09-09T12-00-00Z"


class TestWriteOnce:
    def test_creates_and_writes(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        rd.write("spec.json", {"a": 1})
        assert json.loads((rd.path / "spec.json").read_text()) == {"a": 1}

    def test_refuses_a_duplicate_run_id(self, tmp_path):
        rid = run_id("cond", 1)
        RunDir.create(tmp_path, rid)
        with pytest.raises(RunDirError, match="already exists"):
            RunDir.create(tmp_path, rid)

    def test_refuses_to_overwrite_a_file(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        rd.write("a.json", {"v": 1})
        with pytest.raises(RunDirError, match="refusing to overwrite"):
            rd.write("a.json", {"v": 2})
        assert rd.read("a.json") == {"v": 1}

    def test_overwrite_is_possible_but_explicit(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        rd.write("a.json", {"v": 1})
        rd.write("a.json", {"v": 2}, allow_overwrite=True)
        assert rd.read("a.json") == {"v": 2}

    def test_leaves_no_temp_files_behind(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        rd.write("a.json", {"v": 1})
        assert not list(rd.path.glob("*.tmp"))


class TestSerialization:
    def test_writes_pydantic_models(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        spec = WindowSpec(
            run_id="r", problem_id="p", variant="10%", label="fault",
            namespace="astronomy-shop",
            t_start=datetime(2026, 9, 9, tzinfo=timezone.utc),
            t_end=datetime(2026, 9, 9, 0, 10, tzinfo=timezone.utc),
        )
        rd.write("spec.json", spec)
        assert rd.read("spec.json")["variant"] == "10%"

    def test_handles_values_json_cannot_encode(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        rd.write("t.json", {"when": datetime(2026, 9, 9, tzinfo=timezone.utc)})
        assert "2026-09-09" in rd.read("t.json")["when"]


class TestCompletionMarker:
    def test_incomplete_until_finalized(self, tmp_path):
        rd = RunDir.create(tmp_path, run_id("cond", 1))
        rd.write("partial.json", {})
        assert not rd.is_complete()
        rd.finalize({"status": "ok"})
        assert rd.is_complete()
        assert (rd.path / MARKER).exists()

    def test_crashed_runs_are_identifiable(self, tmp_path):
        done = RunDir.create(tmp_path, run_id("cond", 1))
        done.finalize({"status": "ok"})
        crashed = RunDir.create(tmp_path, run_id("cond", 2))
        crashed.write("normal.json", {})

        assert len(iter_runs(tmp_path)) == 2
        assert len(iter_runs(tmp_path, complete_only=True)) == 1
        assert [r.path for r in incomplete_runs(tmp_path)] == [crashed.path]

    def test_archaeology_directories_are_skipped(self, tmp_path):
        (tmp_path / "_archaeology").mkdir()
        RunDir.create(tmp_path, run_id("cond", 1)).finalize({})
        assert len(iter_runs(tmp_path)) == 1

    def test_empty_or_missing_root(self, tmp_path):
        assert iter_runs(tmp_path / "nope") == []
        assert iter_runs(tmp_path) == []
