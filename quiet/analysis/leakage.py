"""Turn a directory of runs into the numbers PREREG asks for.

PURE MODULE apart from reading files. No cluster, no network, no API key.

Every rule here was fixed before the data existed; this file only applies
them. In particular it does not decide anything PREREG left open:

* **No-verdict is the default** (§7.4). When the 95% CI of an arm
  difference contains zero the report says so in those words. There is no
  code path that emits "a trend is visible".
* **Leaky runs are not dropped** (§4.1). Dropping them would leave only
  the agents that reason from telemetry -- the population we are measuring
  would be gone. Every rate is reported twice: over all runs, and over the
  runs where the answer arrived after the submission (or never).
* **Discards are a reported number** (§7.1), not a silent retry.
* **The null arm sets the false-positive rate** (§7.2).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..harness.rundir import iter_runs
from ..probe.stats import two_proportion_z, wilson_interval, z_to_p_one_sided

#: Problems with no fault injected. Their "a fault is present" answers are
#: the false-positive rate everything else is read against.
NULL_PROBLEMS = ("noop_detection_astronomy_shop-1",)


@dataclass
class Run:
    run_id: str
    problem_id: str
    arm: str
    status: str
    correct: bool | None
    leaked: bool
    intent_only: bool
    censored: bool
    steps: int | None
    cost_usd: float
    blocked: int

    @property
    def clean(self) -> bool:
        """Did this run reach its answer without the answer reaching it?

        A leak *after* the submission cannot have informed it, so such a
        run still counts as clean -- that is what ``leak_censored`` is for.
        """
        return not self.censored


def _num(results: Any, key: str) -> Any:
    return results.get(key) if isinstance(results, dict) else None


def load(root: Path) -> tuple[list[Run], list[dict]]:
    """Read every finished run. Returns (runs, discards)."""
    runs: list[Run] = []
    for rd in iter_runs(root, complete_only=True):
        marker = rd.read("run.json")
        if marker.get("status") != "ok":
            continue
        leak = rd.read("leak.json")
        usage = rd.read("usage.json")
        session = rd.read("session.json")
        results = session.get("results") if isinstance(session, dict) else None
        acc = _num(results, "Detection Accuracy")
        runs.append(Run(
            run_id=marker["run_id"],
            problem_id=marker["problem_id"],
            arm=marker["arm"],
            status=marker["status"],
            correct=None if acc is None else (acc == "Correct"),
            leaked=bool(leak.get("leaked")),
            intent_only=bool(leak.get("intent_only")),
            censored=bool(leak.get("leak_censored")),
            steps=leak.get("submit_step"),
            cost_usd=float(usage.get("cost_usd", 0.0)),
            blocked=int(marker.get("blocked_actions") or 0),
        ))

    discards: list[dict] = []
    path = root / "discarded.jsonl"
    if path.exists():
        discards = [json.loads(line) for line in path.read_text().splitlines() if line]
    return runs, discards


@dataclass
class Rate:
    k: int
    n: int
    lo: float = 0.0
    hi: float = 0.0

    def __post_init__(self) -> None:
        if self.n:
            self.lo, self.hi = wilson_interval(self.k, self.n)

    @property
    def p(self) -> float:
        return self.k / self.n if self.n else float("nan")

    def __str__(self) -> str:
        if not self.n:
            return "     -     "
        return f"{self.p:5.1%} [{self.lo:.2f},{self.hi:.2f}] {self.k}/{self.n}"


def rate(runs: Iterable[Run], pred) -> Rate:
    rs = list(runs)
    return Rate(sum(1 for r in rs if pred(r)), len(rs))


@dataclass
class Comparison:
    """One observe-vs-block contrast, reported under §7.4."""

    label: str
    observe: Rate
    block: Rate
    z: float | None = None
    diff_lo: float = 0.0
    diff_hi: float = 0.0

    def __post_init__(self) -> None:
        if not (self.observe.n and self.block.n):
            return
        self.z = two_proportion_z(
            self.observe.k, self.observe.n, self.block.k, self.block.n
        )
        # Newcombe: the CI of a difference of proportions built from the two
        # Wilson intervals. Behaves at 0 and 1, where the normal-approximation
        # interval does not -- and 0/n is a live possibility for the block arm.
        d = self.observe.p - self.block.p
        self.diff_lo = d - ((self.observe.p - self.observe.lo) ** 2
                            + (self.block.hi - self.block.p) ** 2) ** 0.5
        self.diff_hi = d + ((self.observe.hi - self.observe.p) ** 2
                            + (self.block.p - self.block.lo) ** 2) ** 0.5

    @property
    def verdict(self) -> str:
        if self.z is None:
            return "no data"
        if self.diff_lo <= 0.0 <= self.diff_hi:
            # PREREG 7.4, verbatim. Not "a trend is visible".
            return "no verdict: the 95% CI of the difference contains 0"
        direction = "higher" if self.diff_lo > 0 else "lower"
        return (f"observe is {direction} "
                f"(diff {self.diff_hi if self.diff_hi < 0 else self.diff_lo:+.1%}"
                f"..{self.diff_lo if self.diff_hi < 0 else self.diff_hi:+.1%}, "
                f"p={z_to_p_one_sided(abs(self.z)):.4f})")


def order_trend(runs: list[Run]) -> str:
    """§7.3. Split the arm in half by run order and compare accuracy.

    A campaign is a sequence against one cluster, so drift is the obvious
    way independence breaks. This will not detect a subtle trend at N=30;
    it is here to catch the gross one, and to make the check visible even
    when it passes.
    """
    scored = [r for r in sorted(runs, key=lambda r: r.run_id) if r.correct is not None]
    if len(scored) < 4:
        return "too few runs to check"
    half = len(scored) // 2
    first, second = Rate(sum(r.correct for r in scored[:half]), half), \
        Rate(sum(r.correct for r in scored[half:]), len(scored) - half)
    c = Comparison("order", first, second)
    if c.verdict.startswith("no verdict"):
        return f"no drift detected (first {first.p:.0%} vs last {second.p:.0%})"
    return f"★ DRIFT: first half {first.p:.0%} vs last half {second.p:.0%} -- {c.verdict}"


def report(root: Path) -> str:
    runs, discards = load(root)
    out: list[str] = []
    w = out.append

    w(f"# leakage report -- {root}")
    w("")
    if not runs:
        w("No completed runs. Nothing to report.")
        return "\n".join(out)

    arms = sorted({r.arm for r in runs})
    by_arm = {a: [r for r in runs if r.arm == a] for a in arms}
    fault = [r for r in runs if r.problem_id not in NULL_PROBLEMS]
    null = [r for r in runs if r.problem_id in NULL_PROBLEMS]

    # -- 7.1 discards ------------------------------------------------------
    w("## discarded (PREREG 7.1)")
    w("")
    total = len(runs) + len(discards)
    w(f"{len(discards)}/{total} runs discarded before measurement "
      f"({len(discards) / total:.0%})." if total else "none")
    phases: dict[str, int] = defaultdict(int)
    for d in discards:
        phases[d.get("phase", "?")] += 1
    for phase, n in sorted(phases.items()):
        w(f"  - {phase}: {n}")
    w("")

    # -- headline ----------------------------------------------------------
    w("## rates by arm")
    w("")
    w("| arm | n | leaked | intent only | correct | correct (clean only) |")
    w("|---|---:|---|---|---|---|")
    for a in arms:
        rs = by_arm[a]
        clean = [r for r in rs if r.clean]
        w(f"| {a} | {len(rs)} | {rate(rs, lambda r: r.leaked)} "
          f"| {rate(rs, lambda r: r.intent_only)} "
          f"| {rate(rs, lambda r: r.correct is True)} "
          f"| {rate(clean, lambda r: r.correct is True)} |")
    w("")
    w("`clean only` = runs where the answer never arrived, or arrived after the")
    w("submission. Leaky runs are NOT dropped (PREREG 4.1) -- both columns are")
    w("the result.")
    w("")

    # -- 7.2 false positives ----------------------------------------------
    w("## false-positive rate (PREREG 7.2)")
    w("")
    if not null:
        w("No null-arm runs. Detection rates are raw and uncorrected -- say so.")
    else:
        fp = rate(null, lambda r: r.correct is False)  # said "fault" when none
        w(f"null problems: {fp}")
        w("")
        w("Detection rates above are uncorrected. With a non-zero false-positive")
        w("rate, report corrected and uncorrected together.")
    w("")

    # -- the contrast ------------------------------------------------------
    if len(arms) >= 2 and "observe" in by_arm and "block" in by_arm:
        w("## observe vs block (PREREG 7.4)")
        w("")
        o, b = by_arm["observe"], by_arm["block"]
        of = [r for r in o if r.problem_id not in NULL_PROBLEMS]
        bf = [r for r in b if r.problem_id not in NULL_PROBLEMS]
        for label, pred, oo, bb in (
            ("H1 leak rate", lambda r: r.leaked, of, bf),
            ("H2 accuracy", lambda r: r.correct is True, of, bf),
        ):
            c = Comparison(label, rate(oo, pred), rate(bb, pred))
            w(f"**{label}** -- observe {c.observe} vs block {c.block}")
            w("")
            w(f"> {c.verdict}")
            w("")
    else:
        w("## observe vs block")
        w("")
        w(f"Only one arm present ({', '.join(arms)}). No contrast to report.")
        w("")

    # -- 7.3 independence --------------------------------------------------
    w("## independence (PREREG 7.3)")
    w("")
    for a in arms:
        w(f"- {a}: {order_trend(by_arm[a])}")
    w("")

    # -- per problem -------------------------------------------------------
    w("## per problem")
    w("")
    w("| problem | arm | n | leaked | correct | median step | $/run |")
    w("|---|---|---:|---|---|---:|---:|")
    for pid in sorted({r.problem_id for r in runs}):
        for a in arms:
            rs = [r for r in runs if r.problem_id == pid and r.arm == a]
            if not rs:
                continue
            steps = sorted(r.steps for r in rs if r.steps is not None)
            med = steps[len(steps) // 2] if steps else None
            w(f"| {pid} | {a} | {len(rs)} | {rate(rs, lambda r: r.leaked)} "
              f"| {rate(rs, lambda r: r.correct is True)} "
              f"| {med if med is not None else '-'} "
              f"| {sum(r.cost_usd for r in rs) / len(rs):.3f} |")
    w("")
    w(f"**total spent: ${sum(r.cost_usd for r in runs):.2f} over {len(runs)} runs**")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path, nargs="?", default=Path("runs"))
    ap.add_argument("-o", "--out", type=Path, help="also write the report here")
    args = ap.parse_args(argv)

    text = report(args.root)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
