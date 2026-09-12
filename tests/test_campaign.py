"""Campaign-runner tests that need no cluster.

The reset checks take a kubectl-like object, so a fake one exercises every
path. The fake mirrors the *real* ``KubeCtl.exec_command`` contract: on
failure it returns stderr as an ordinary string unless ``raise_on_error`` is
set. The previous fake raised unconditionally, which is why a full green
suite still let pilot 3 discard a clean run -- the fake was wrong about the
one behaviour that mattered.
"""

from __future__ import annotations

import json

import pytest

from quiet.harness.leak import blocks_action
from quiet.harness.run_campaign import (
    BLOCKED_MSG,
    EXPECT,
    PROBLEMS,
    verify_fault,
    verify_reset,
)

PAYMENT = "astronomy_shop_payment_service_failure-detection-1"
PODKILL = "pod_kill_hotel_res-detection-1"
NOOP = "noop_detection_astronomy_shop-1"

NO_CHAOS_CRD = 'error: the server doesn\'t have a resource type "podchaos"'


class FakeKubectl:
    """Answers the commands the reset checks issue.

    ``chaos`` and ``flags`` may be an ``Err`` to simulate a failing command.
    """

    class Err(str):
        """stderr text that kubectl would have written."""

    def __init__(self, chaos="", flags=None):
        self._chaos = chaos
        self._flags = flags

    def _answer(self, value, raise_on_error):
        if isinstance(value, FakeKubectl.Err):
            if raise_on_error:
                raise RuntimeError(f"Command failed\nError: {value}")
            return str(value)  # the real contract: stderr comes back as data
        return value

    def exec_command(self, cmd: str, raise_on_error: bool = False) -> str:
        if "chaos" in cmd:
            return self._answer(self._chaos, raise_on_error)
        if "flagd-config" in cmd:
            if self._flags is None:
                return self._answer(
                    FakeKubectl.Err('Error from server (NotFound): configmaps '
                                    '"flagd-config" not found'),
                    raise_on_error,
                )
            if isinstance(self._flags, FakeKubectl.Err):
                return self._answer(self._flags, raise_on_error)
            payload = json.dumps({"flags": self._flags})
            return json.dumps({"data": {"demo.flagd.json": payload}})
        raise AssertionError(f"unexpected command: {cmd}")


class TestVerifyReset:
    def test_clean_cluster_passes(self):
        ok, issues = verify_reset(FakeKubectl())
        assert ok and issues == []

    def test_missing_chaos_crds_are_not_contamination(self):
        """The pilot-3 regression. Chaos Mesh is not installed in the lab
        cluster, and kubectl says so on stderr; the old check counted the
        nine words of that sentence as nine leftover CRs."""
        ok, issues = verify_reset(FakeKubectl(chaos=FakeKubectl.Err(NO_CHAOS_CRD)))
        assert ok, issues

    def test_leftover_chaos_cr_fails(self):
        k = FakeKubectl(chaos="podchaos.chaos-mesh.org/pod-kill\n")
        ok, issues = verify_reset(k)
        assert not ok and "chaos CRs left over: 1" in issues[0]

    def test_unreachable_cluster_fails_closed(self):
        """A CRD that is absent is safe; an API server that will not answer
        is not, and the two arrive as the same kind of failure."""
        k = FakeKubectl(chaos=FakeKubectl.Err("Unable to connect to the server"))
        ok, issues = verify_reset(k)
        assert not ok and "cannot list chaos CRs" in issues[0]

    def test_does_not_look_at_the_app_namespace(self):
        """init_problem uninstalls and recreates it, so its prior contents
        are not evidence -- checking them would discard every run after the
        first."""
        k = FakeKubectl(flags={"paymentFailure": {"defaultVariant": "100%"}})
        ok, _ = verify_reset(k)
        assert ok


class TestVerifyFault:
    def test_expected_flag_on_passes(self):
        k = FakeKubectl(flags={"paymentFailure": {"defaultVariant": "100%"},
                               "cartFailure": {"defaultVariant": "off"}})
        ok, issues = verify_fault(k, "ns", PAYMENT)
        assert ok, issues

    def test_silent_no_op_injection_is_caught(self):
        """Nothing upstream checks that inject_fault landed. A healthy system
        scored as an agent miss is the failure that would fake this result."""
        k = FakeKubectl(flags={"paymentFailure": {"defaultVariant": "off"}})
        ok, issues = verify_fault(k, "ns", PAYMENT)
        assert not ok and "did not materialise" in issues[0]

    def test_extra_flag_from_a_previous_run_fails(self):
        k = FakeKubectl(flags={"paymentFailure": {"defaultVariant": "100%"},
                               "imageSlowLoad": {"defaultVariant": "10sec"}})
        ok, issues = verify_fault(k, "ns", PAYMENT)
        assert not ok and any("imageSlowLoad" in i for i in issues)

    def test_null_arm_requires_every_flag_off(self):
        assert verify_fault(FakeKubectl(flags={}), "ns", NOOP)[0]
        ok, issues = verify_fault(
            FakeKubectl(flags={"paymentFailure": {"defaultVariant": "10%"}}), "ns", NOOP
        )
        assert not ok and "unexpected flags on" in issues[0]

    def test_chaos_problem_wants_a_cr_and_no_flagd(self):
        """hotel-reservation has no flagd ConfigMap; that absence is not a
        failure when no flag was expected."""
        k = FakeKubectl(chaos="podchaos.chaos-mesh.org/pod-kill\n", flags=None)
        assert verify_fault(k, "ns", PODKILL)[0]

        ok, issues = verify_fault(FakeKubectl(chaos="", flags=None), "ns", PODKILL)
        assert not ok and "expected a chaos CR" in issues[0]

    def test_missing_flagd_fails_when_a_flag_was_expected(self):
        ok, issues = verify_fault(FakeKubectl(flags=None), "ns", PAYMENT)
        assert not ok and "cannot read flagd-config" in issues[0]

    def test_unregistered_problem_fails_closed(self):
        ok, issues = verify_fault(FakeKubectl(), "ns", "something-new-1")
        assert not ok and "no expectation registered" in issues[0]

    def test_every_planned_problem_has_an_expectation(self):
        assert set(EXPECT) == set(PROBLEMS)


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
