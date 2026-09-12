"""Exercise the agent loop without a cluster, an API key, or money.

`_drive_agent` is the one path the block arm depends on and it has never
executed -- the observe arm does not take the blocked branch. Finding a
wiring bug thirty runs into the campaign would cost an hour of cluster
time, so it is driven here with fakes instead.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

import pytest

from quiet.harness.budget import Budget, BudgetExceeded, BudgetLedger, load_pricing
from quiet.harness.run_campaign import BLOCKED_MSG, PRICING_PATH, _drive_agent


class VALID:
    """Stands in for aiopslab's SubmissionStatus.VALID_SUBMISSION."""


@pytest.fixture(autouse=True)
def fake_aiopslab(monkeypatch):
    """`_drive_agent` imports SubmissionStatus from the checkout.

    Stubbing the module keeps this test runnable on a laptop -- importing
    the real one reads a gitignored config.yml and loads a kubeconfig.
    """
    mod = types.ModuleType("aiopslab.utils.status")
    mod.SubmissionStatus = types.SimpleNamespace(
        VALID_SUBMISSION=VALID, INVALID_SUBMISSION=object()
    )
    for name in ("aiopslab", "aiopslab.utils", "aiopslab.utils.status"):
        monkeypatch.setitem(sys.modules, name,
                            mod if name.endswith("status") else types.ModuleType(name))
    monkeypatch.setattr("quiet.paths.ensure_importable", lambda: None)
    yield


class FakeSession:
    def __init__(self):
        self.entries = []

    def add(self, item):
        self.entries.append(item)

    @property
    def roles(self):
        return [e["role"] for e in self.entries]


class FakeAgent:
    """Replays a fixed script of actions and reports token usage.

    Accumulates into one dict per call, exactly as ClaudeClient._record
    does (`self.usage[k] += ...`, clients/claude.py:94). The loop drains
    that accumulator each turn, so a fake that populated it once would
    make four calls look like one and hide any double- or under-charging.
    """

    PER_CALL = {"input_tokens": 10, "output_tokens": 5,
                "cache_read_tokens": 0, "cache_write_tokens": 0}

    def __init__(self, actions):
        self._actions = list(actions)
        self.llm = types.SimpleNamespace(usage={"calls": 0, **dict.fromkeys(self.PER_CALL, 0)})
        self.prompts = []

    async def get_action(self, instr):
        self.prompts.append(instr)
        self.llm.usage["calls"] += 1
        for k, v in self.PER_CALL.items():
            self.llm.usage[k] += v
        return self._actions.pop(0) if self._actions else 'exec_shell("true")'


class FakeOrch:
    def __init__(self, session, *, submit_on=None):
        self.session = session
        self.asked = []
        self._submit_on = submit_on

    async def ask_env(self, action):
        self.asked.append(action)
        resp = VALID if (self._submit_on and self._submit_on in action) else "ok"
        # The real ask_env records its own entry; the loop must not add a
        # second one (see DEFERRED.md / orchestrator.py:141).
        self.session.add({"role": "env", "content": str(resp)})
        return resp


def drive(actions, *, block, submit_on=None, max_steps=6, budget=10.0,
          root=None):
    session = FakeSession()
    agent = FakeAgent(actions)
    orch = FakeOrch(session, submit_on=submit_on)
    root = root or Path(tempfile.mkdtemp())
    ledger = BudgetLedger(root / "ledger.json",
                          Budget(max_usd=budget, reserve_per_run_usd=0.0),
                          load_pricing(PRICING_PATH))
    out = asyncio.run(_drive_agent(orch, agent, session, max_steps, block=block,
                                   ledger=ledger, rid="r", model="claude-opus-5"))
    return out, session, orch, ledger


READ_ANSWER = 'exec_shell("kubectl get cm flagd-config -n astronomy-shop -o yaml")'
ORDINARY = 'exec_shell("kubectl get pods -n astronomy-shop")'


class TestObserveArm:
    def test_nothing_is_intercepted(self):
        out, _, orch, _ = drive([ORDINARY, READ_ANSWER], block=False, max_steps=2)
        assert out["blocked_actions"] == 0
        assert orch.asked == [ORDINARY, READ_ANSWER]

    def test_submission_ends_the_loop_early(self):
        out, session, orch, _ = drive(
            [ORDINARY, 'submit("Yes")'], block=False, submit_on="submit", max_steps=6)
        assert len(orch.asked) == 2
        assert out["final_state"] == str(VALID)


class TestBlockArm:
    def test_the_answer_read_never_reaches_the_environment(self):
        """The whole point: a blocked action must not execute."""
        out, _, orch, _ = drive([READ_ANSWER, ORDINARY], block=True, max_steps=2)
        assert out["blocked_actions"] == 1
        assert READ_ANSWER not in orch.asked
        assert ORDINARY in orch.asked

    def test_the_refusal_is_what_the_agent_sees_next(self):
        _, session, _, _ = drive([READ_ANSWER], block=True, max_steps=1)
        assert session.entries[-1] == {"role": "env", "content": BLOCKED_MSG}

    def test_the_refusal_is_fed_back_as_the_next_instruction(self):
        """A refusal the agent never reads teaches it nothing -- it would
        just retry the same command until max_steps."""
        session = FakeSession()
        agent = FakeAgent([READ_ANSWER, ORDINARY])
        orch = FakeOrch(session)
        ledger = BudgetLedger(Path(tempfile.mkdtemp()) / "l.json",
                              Budget(max_usd=10.0, reserve_per_run_usd=0.0),
                              load_pricing(PRICING_PATH))
        asyncio.run(_drive_agent(orch, agent, session, 2, block=True,
                                 ledger=ledger, rid="r", model="claude-opus-5"))
        assert BLOCKED_MSG in agent.prompts[1]

    def test_ordinary_investigation_is_untouched(self):
        out, _, orch, _ = drive([ORDINARY] * 3, block=True, max_steps=3)
        assert out["blocked_actions"] == 0
        assert len(orch.asked) == 3

    def test_a_submission_is_never_blocked(self):
        out, _, orch, _ = drive(['submit("Yes")'], block=True,
                                submit_on="submit", max_steps=2)
        assert out["blocked_actions"] == 0
        assert orch.asked == ['submit("Yes")']


class TestTraceShape:
    def test_one_env_entry_per_turn_in_both_arms(self):
        """Arms whose traces have different shapes are not comparable."""
        _, obs, _, _ = drive([ORDINARY] * 3, block=False, max_steps=3)
        _, blk, _, _ = drive([READ_ANSWER] * 3, block=True, max_steps=3)
        assert obs.roles == ["assistant", "env"] * 3
        assert blk.roles == ["assistant", "env"] * 3


class TestBudget:
    def test_usage_is_charged_per_call_not_at_the_end(self):
        """A crash mid-run must not lose the accounting."""
        _, _, _, ledger = drive([ORDINARY] * 3, block=False, max_steps=3)
        assert ledger.ledger.spent_usd > 0

    def test_each_call_is_charged_once_and_only_once(self):
        """The loop zeroes the accumulator after reading it. Too little
        zeroing double-charges; too much loses a turn."""
        one = drive([ORDINARY], block=False, max_steps=1)[3].ledger.spent_usd
        four = drive([ORDINARY] * 4, block=False, max_steps=4)[3].ledger.spent_usd
        assert four == pytest.approx(one * 4, rel=1e-9)

    def test_the_loop_stops_when_the_budget_runs_out(self):
        with pytest.raises(BudgetExceeded):
            drive([ORDINARY] * 50, block=False, max_steps=50, budget=0.0005)
