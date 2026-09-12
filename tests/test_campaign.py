"""Campaign-runner tests that need no cluster.

`verify_clean` takes a kubectl-like object, so a fake one exercises every
contamination path. The block hook and the resume rule are checked directly.
"""

from __future__ import annotations

import json

import pytest

from quiet.harness.leak import blocks_action
from quiet.harness.run_campaign import BLOCKED_MSG, PROBLEMS, verify_clean


class FakeKubectl:
    """Answers the three commands verify_clean issues."""

    def __init__(self, pods=None, chaos="", flags=None, fail=None):
        self._pods = pods if pods is not None else [self.pod("a")]
        self._chaos = chaos
        self._flags = flags
        self._fail = fail or set()

    @staticmethod
    def pod(name, ready=True, restarts=0):
        return {
            "metadata": {"name": name},
            "status": {"containerStatuses": [
                {"name": "c", "ready": ready, "restartCount": restarts}
            ]},
        }

    def exec_command(self, cmd: str) -> str:
        if "get pods" in cmd:
            if "pods" in self._fail:
                raise RuntimeError("connection refused")
            return json.dumps({"items": self._pods})
        if "chaos" in cmd:
            if "chaos" in self._fail:
                raise RuntimeError("no chaos CRDs")
            return self._chaos
        if "flagd-config" in cmd:
            if self._flags is None:
                raise RuntimeError("configmap not found")
            return json.dumps({"data": {"demo.flagd.json": json.dumps({"flags": self._flags})}})
        raise AssertionError(f"unexpected command: {cmd}")


class TestVerifyClean:
    def test_healthy_namespace_passes(self):
        ok, issues = verify_clean(FakeKubectl(), "ns")
        assert ok and issues == []

    def test_empty_namespace_fails(self):
        """An empty namespace means the app never deployed -- scoring that as
        a clean baseline would make every fault look explicit."""
        ok, issues = verify_clean(FakeKubectl(pods=[]), "ns")
        assert not ok and "empty" in issues[0]

    def test_unreachable_cluster_fails_closed(self):
        ok, issues = verify_clean(FakeKubectl(fail={"pods"}), "ns")
        assert not ok and "cannot list pods" in issues[0]

    def test_not_ready_pod_fails(self):
        k = FakeKubectl(pods=[FakeKubectl.pod("a"), FakeKubectl.pod("b", ready=False)])
        ok, issues = verify_clean(k, "ns")
        assert not ok and any("not ready: b" in i for i in issues)

    def test_restart_count_fails(self):
        """A baseline that is already churning contaminates the event channel."""
        k = FakeKubectl(pods=[FakeKubectl.pod("a", restarts=2)])
        ok, issues = verify_clean(k, "ns")
        assert not ok and any("restarts=2" in i for i in issues)

    def test_leftover_chaos_cr_fails(self):
        k = FakeKubectl(chaos="podchaos.chaos-mesh.org/pod-kill\n")
        ok, issues = verify_clean(k, "ns")
        assert not ok and any("chaos CRs" in i for i in issues)

    def test_flag_left_on_fails(self):
        """The likeliest contamination: the previous run's recover_fault
        silently failed and its feature flag is still set."""
        k = FakeKubectl(flags={"paymentFailure": {"defaultVariant": "100%"},
                               "cartFailure": {"defaultVariant": "off"}})
        ok, issues = verify_clean(k, "ns")
        assert not ok
        assert any("paymentFailure" in i for i in issues)
        assert not any("cartFailure" in i for i in issues)

    def test_all_flags_off_passes(self):
        k = FakeKubectl(flags={"paymentFailure": {"defaultVariant": "off"}})
        ok, issues = verify_clean(k, "ns")
        assert ok, issues

    def test_missing_optional_sources_are_not_failures(self):
        """hotel-reservation has no flagd and a cluster may have no Chaos Mesh;
        neither absence is contamination."""
        ok, issues = verify_clean(FakeKubectl(fail={"chaos"}, flags=None), "ns")
        assert ok, issues

    def test_reports_every_problem_not_just_the_first(self):
        k = FakeKubectl(
            pods=[FakeKubectl.pod("a", ready=False, restarts=3)],
            chaos="podchaos/x\n",
            flags={"f": {"defaultVariant": "on"}},
        )
        ok, issues = verify_clean(k, "ns")
        assert not ok and len(issues) >= 3


class TestBlockHook:
    def test_blocks_the_answer_reads(self):
        assert blocks_action('exec_shell("kubectl get cm flagd-config -o yaml")')
        assert blocks_action('exec_shell("kubectl get podchaos -A")')

    def test_leaves_ordinary_investigation_alone(self):
        for cmd in ('exec_shell("kubectl get pods -n astronomy-shop")',
                    'exec_shell("kubectl logs payment-0")',
                    'get_traces("astronomy-shop", 5)',
                    'submit("Yes")'):
            assert not blocks_action(cmd), cmd

    def test_refusal_message_does_not_advertise_the_answer(self):
        """A message naming flagd or the fault would tell the agent exactly
        where to look -- the block would then leak what it prevents."""
        lowered = BLOCKED_MSG.lower()
        for word in ("flagd", "chaos", "configmap", "fault", "answer", "variant"):
            assert word not in lowered, word


class TestPlan:
    def test_problem_set_matches_the_prereg(self):
        assert len(PROBLEMS) == 5
        assert sum("noop" in p for p in PROBLEMS) == 1, "exactly one null arm"
        assert sum("pod_kill" in p for p in PROBLEMS) == 1, "exactly one loud control"
        assert sum(p.startswith("astronomy_shop") for p in PROBLEMS) == 3

    def test_every_problem_is_a_detection_task(self):
        """get_problem_ids filters by substring; a non-detection id here would
        silently measure a different task."""
        assert all("detection" in p for p in PROBLEMS)


class TestResumeRule:
    def test_matches_on_the_stable_part_of_the_run_id(self, tmp_path):
        """Run ids embed a timestamp, so resume cannot compare them directly."""
        from quiet.harness.rundir import RunDir, run_id

        rid = run_id("astronomy_shop_image_slow_load-detection-1__observe", 3)
        RunDir.create(tmp_path, rid).finalize({"status": "ok"})

        done = {d.name for d in tmp_path.iterdir()
                if d.is_dir() and (d / "run.json").exists()}
        stem = "astronomy_shop_image_slow_load-detection-1__observe__r03"
        assert any(d.endswith(stem) for d in done)
        assert not any(d.endswith(stem.replace("r03", "r04")) for d in done)

    def test_unfinished_run_is_not_skipped(self, tmp_path):
        from quiet.harness.rundir import RunDir, run_id

        rid = run_id("p__observe", 1)
        RunDir.create(tmp_path, rid).write("session.json", {})  # no run.json
        done = {d.name for d in tmp_path.iterdir()
                if d.is_dir() and (d / "run.json").exists()}
        assert done == set()
