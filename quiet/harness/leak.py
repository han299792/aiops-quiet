"""Detect when the agent read the answer instead of inferring it.

PURE MODULE. Operates on a session trace (a list of ``{"role", "content"}``
dicts, exactly the ``trace`` field of AIOpsLab's session JSON).

Why this is necessary: the agent's ``exec_shell`` action runs arbitrary
shell. Its blocklist (``aiopslab/orchestrator/actions/base.py``) contains
five interactive commands and nothing else -- no namespace allowlist, no
resource filter. So the fault is directly readable::

    kubectl get cm flagd-config -n astronomy-shop -o yaml
      -> "paymentFailure": {"defaultVariant": "50%"}
    kubectl get podchaos,networkchaos -A
      -> the chaos experiments, named after the fault

The distinction that makes this measurement sound is between what the
agent TRIED and what actually reached its context:

* An agent that ran ``kubectl get cm -A -o yaml`` had no intent to cheat
  but received the answer anyway. That is a leak.
* An agent whose ``kubectl get cm flagd-config`` errored out learned
  nothing. That is intent, not a leak.

Only observations are decisive. Intent is reported separately because
"how often does the agent try to read the answer" is its own finding
about benchmark hygiene.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Family = Literal["action", "observation"]
Severity = Literal["direct", "indirect"]

_I = re.IGNORECASE


class Rule(BaseModel, frozen=True):
    name: str
    family: Family
    severity: Severity
    pattern: str

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, _I)


# What the agent typed. Evidence of intent, not of leakage.
ACTION_RULES: tuple[Rule, ...] = (
    Rule(name="flagd_configmap", family="action", severity="direct",
         pattern=r"flagd-config|demo\.flagd\.json|configmap\s+flagd|\bcm\s+flagd"),
    Rule(name="flagd_workload", family="action", severity="direct",
         pattern=r"deployment[/\s]+flagd|deploy\s+flagd|app=flagd|describe\s+\S*flagd"),
    Rule(name="chaos_resources", family="action", severity="direct",
         pattern=r"podchaos|networkchaos|stresschaos|iochaos|httpchaos|kernelchaos|chaos-?mesh"),
    Rule(name="flagd_events", family="action", severity="direct",
         pattern=r"get\s+events[^\n]*flagd|involvedObject\.name=flagd"),
    Rule(name="injection_yaml", family="action", severity="direct",
         pattern=r"/tmp/(pod-kill|pod-failure|container-kill|network-delay|network-loss|kernel-chaos)\.yaml"),
    Rule(name="helm_values", family="action", severity="indirect",
         pattern=r"helm\s+(get\s+values|get\s+manifest|history)"),
    Rule(name="configmap_sweep", family="action", severity="indirect",
         pattern=r"get\s+(cm|configmaps?)\b[^\n]*(-A|--all-namespaces)[^\n]*-o\s*(yaml|json)"),
)

# What came back into the context. This is what actually leaks.
#
# CAREFUL: the flagd ConfigMap lists *every* flag with its description and
# its variant, whichever one is armed. So "the flag name appeared" and
# "the string defaultVariant appeared" are properties of reading the
# ConfigMap at all, not of learning the answer -- the first version of
# these rules matched `"defaultVariant": "off"` on the innocuous flag at
# the top of the dump and reported a leak for it. That would have marked
# every flagd read as a leak, including reads in the null arm where
# nothing is armed, and collapsed the contrast the experiment rests on.
#
# The answer is specifically *a flag set to something other than off*.
OBSERVATION_RULES: tuple[Rule, ...] = (
    Rule(name="armed_variant", family="observation", severity="direct",
         # No trailing \b: it would sit between `%` and `"`, two non-word
         # characters, and never match the commonest value of all.
         pattern=r"\"?defaultVariant\"?\s*[:=]\s*\"?(?!off)(\d+%|\d+sec|on(?![\w-]))"),
    Rule(name="chaos_manifest", family="observation", severity="direct",
         pattern=r"kind:\s*(Pod|Network|Stress|IO|HTTP|Kernel)Chaos"),
    # Reading the ConfigMap and finding everything off is not the answer
    # when a fault is armed -- but it *is* the answer in the null arm.
    # Recorded as indirect here; scan_trace promotes it when no fault was
    # injected.
    Rule(name="flagd_dump", family="observation", severity="indirect",
         pattern=r"\"?defaultVariant\"?\s*[:=]"),
)

ALL_RULES: tuple[Rule, ...] = ACTION_RULES + OBSERVATION_RULES

_ROLE_FAMILY: dict[str, Family] = {"assistant": "action", "env": "observation"}

_EXCERPT_CHARS = 160


class FaultSpec(BaseModel, frozen=True):
    """What this run actually injected.

    Needed because what counts as "the answer" depends on it. With a flag
    armed, the answer is a non-off variant. In the null arm nothing is
    armed, so the answer is the *absence* of one -- and an agent that
    dumps the ConfigMap and sees everything off has read it just as
    surely. Defaults to the null arm.
    """

    flag: str | None = None
    chaos: bool = False

    @property
    def any(self) -> bool:
        return bool(self.flag) or self.chaos


class LeakHit(BaseModel):
    step: int
    family: Family
    rule: str
    severity: Severity
    excerpt: str


class LeakReport(BaseModel):
    #: True if the answer actually entered the agent's context.
    leaked: bool = False
    #: True if the agent tried to read the answer but nothing came back.
    intent_only: bool = False
    #: Step index of the first observation-family direct hit. Runs that
    #: submitted BEFORE this are clean and fully usable; see censoring
    #: in PREREG 8.2.
    first_leak_step: int | None = None
    #: Step index at which the agent submitted, if it did.
    submit_step: int | None = None
    #: True when the leak arrived before the submission, i.e. the answer
    #: could have informed it.
    leak_censored: bool = False
    hits: list[LeakHit] = Field(default_factory=list)


_SUBMIT_RE = re.compile(r"\bsubmit\s*\(", _I)


def _excerpt(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - _EXCERPT_CHARS // 2)
    end = min(len(text), match.end() + _EXCERPT_CHARS // 2)
    return text[start:end].replace("\n", " ").strip()


def scan_trace(trace: list[dict], fault: FaultSpec | None = None) -> LeakReport:
    """Scan a session trace for answer leakage.

    ``trace`` items are ``{"role": ..., "content": ...}``. Roles other
    than ``assistant`` and ``env`` (``system``, ``user``) are skipped:
    the task description legitimately names the application and would
    otherwise trip the action rules.

    ``fault`` says what was injected. Omitting it scores the trace as a
    null-arm run, where a bare ConfigMap dump is itself the answer.
    """
    fault = fault or FaultSpec()
    hits: list[LeakHit] = []
    first_leak: int | None = None
    submit_step: int | None = None

    for step, item in enumerate(trace):
        role = str(item.get("role", ""))
        family = _ROLE_FAMILY.get(role)
        if family is None:
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            continue

        if family == "action" and submit_step is None and _SUBMIT_RE.search(content):
            submit_step = step

        for rule in ALL_RULES:
            if rule.family != family:
                continue
            match = rule.compiled().search(content)
            if match is None:
                continue
            severity = rule.severity
            # In the null arm there is no armed variant to find, so the
            # ConfigMap dump is the whole answer.
            if rule.name == "flagd_dump" and not fault.any:
                severity = "direct"

            hits.append(
                LeakHit(
                    step=step,
                    family=family,
                    rule=rule.name,
                    severity=severity,
                    excerpt=_excerpt(content, match),
                )
            )
            if family == "observation" and severity == "direct" and first_leak is None:
                first_leak = step

    leaked = first_leak is not None
    tried = any(h.family == "action" and h.severity == "direct" for h in hits)
    return LeakReport(
        leaked=leaked,
        intent_only=tried and not leaked,
        first_leak_step=first_leak,
        submit_step=submit_step,
        leak_censored=(
            leaked and (submit_step is None or first_leak < submit_step)  # type: ignore[operator]
        ),
        hits=hits,
    )


def blocks_action(action: str) -> bool:
    """Would this action be refused under ``leak_policy="block"``?

    Used only by the blocking ablation arm, never by the default run:
    the natural leak rate has to be measured before it is suppressed.
    Keeping the guard here rather than patching AIOpsLab's own blocklist
    means the upstream action surface stays untouched and comparable.
    """
    return any(
        rule.severity == "direct" and rule.compiled().search(action)
        for rule in ACTION_RULES
    )
