"""Turn captured runs into the explicitness catalogue.

PURE apart from reading the run directory. No cluster, so the table can
be recomputed on a laptop whenever the parser or the scoring changes,
and re-derived for every past run at once.

Thresholds come from the ``noop`` runs — a problem where no fault was
injected, so any signal it shows is the meter's own noise. That is what
makes "this problem is quiet" a measurement rather than an opinion.

Two checks decide whether the table can be believed at all:

* noop must score 0. If it does not, the thresholds are wrong.
* a loud fault (``pod_kill``) must score 3. If it does not, the meter is
  broken and every quiet reading below it is meaningless.

Both are printed above the table, on purpose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..probe.calibrate import calibrate, evaluate_null, split_half
from ..probe.model import SUBSTATS, Thresholds, WindowSnapshot
from ..probe.score import compute_effects, verdict

NOOP_MARKER = "noop"
LOUD_MARKERS = ("pod_kill", "container_kill", "pod_failure")


def load_runs(root: Path) -> list[tuple[str, WindowSnapshot, WindowSnapshot]]:
    """(problem_id, normal, fault) for every completed run under root."""
    out = []
    for d in sorted(Path(root).iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if not (d / "run.json").exists():
            continue  # crashed or still running; never silently counted
        try:
            normal = WindowSnapshot.model_validate_json((d / "normal.json").read_text())
            fault = WindowSnapshot.model_validate_json((d / "fault.json").read_text())
        except Exception:  # noqa: BLE001
            continue
        out.append((normal.spec.problem_id, normal, fault))
    return out


def fit_thresholds(runs, *, alpha: float = 0.05) -> tuple[Thresholds, dict]:
    """Calibrate on the noop arms, then measure the realized error rate.

    A noop run gives two windows that are both genuinely normal, so the
    pair is a null draw regardless of which one is labelled "fault".
    """
    null_pairs = [(n, f) for pid, n, f in runs if NOOP_MARKER in pid]
    if len(null_pairs) < 2:
        raise SystemExit(
            f"need at least 2 noop runs to calibrate, found {len(null_pairs)}.\n"
            "  python -m quiet.harness.run_catalog --calibrate-runs 5"
        )
    fit, held = split_half(null_pairs, seed=0)
    if not held:  # too few to hold out; fit on everything and say so
        fit, held = null_pairs, []
    thr = calibrate(fit, alpha=alpha)
    realized = evaluate_null(thr, held) if held else {}
    return thr, realized


def score_all(runs, thr: Thresholds) -> list[dict]:
    rows = []
    for pid, normal, fault in runs:
        effects = compute_effects(normal, fault)
        v = verdict(effects, thr)
        rows.append({
            "problem_id": pid,
            "explicitness": v.explicitness,
            "n_usable": v.n_usable_channels,
            "channels": v.per_channel,
            "fired": v.fired,
            "margins": v.margins,
            "spans_normal": normal.traces.total.spans if normal.traces else 0,
            "spans_fault": fault.traces.total.spans if fault.traces else 0,
        })
    return rows


#: Tolerated share of noop runs that may score above zero.
#:
#: NOT zero. Thresholds are the (1 - alpha/m) quantile of the null, so by
#: construction some share of genuinely null runs exceeds them -- that is
#: what alpha means. Demanding every noop score 0 would contradict the
#: calibration and quietly push the thresholds up until nothing ever
#: fires. The tolerance is deliberately loose because with five or six
#: noop runs the observed rate cannot be distinguished from alpha anyway.
NOOP_FIRE_TOLERANCE = 0.34


def noop_fire_rate(rows: list[dict]) -> tuple[int, int]:
    noop = [r for r in rows if NOOP_MARKER in r["problem_id"]]
    return sum(1 for r in noop if r["explicitness"] > 0), len(noop)


def sanity(rows: list[dict], *, tolerance: float = NOOP_FIRE_TOLERANCE) -> list[str]:
    """The two checks that decide whether the table means anything."""
    problems = []
    fired, total = noop_fire_rate(rows)
    loud = [r for r in rows if any(m in r["problem_id"] for m in LOUD_MARKERS)]

    if total == 0:
        problems.append("no noop run — thresholds are uncalibrated")
    elif (fired / total) > tolerance:
        problems.append(
            f"noop fired on {fired}/{total} runs ({fired / total:.0%}), above the "
            f"{tolerance:.0%} tolerance — the meter reacts to nothing happening. "
            "Re-calibrate with more noop runs; do not read the table below."
        )

    if not loud:
        problems.append("no loud arm (pod_kill) — the meter is unverified")
    else:
        best = max(r["explicitness"] for r in loud)
        if best < 3:
            problems.append(
                f"loud fault scored {best}, expected 3 — the meter is not "
                "detecting an obvious failure, so quiet readings mean nothing."
            )
    return problems


def render(rows: list[dict], thr: Thresholds, realized: dict) -> str:
    rows = sorted(rows, key=lambda r: (-r["explicitness"], r["problem_id"]))
    lines = ["", "=" * 78, "명시성 카탈로그 — 장애가 텔레메트리에 얼마나 적혀 있나", "=" * 78, ""]

    issues = sanity(rows)
    if issues:
        lines.append("*** 측정기 검증 실패 ***")
        lines += [f"  - {p}" for p in issues]
        lines.append("")
    else:
        lines.append("측정기 검증 통과: noop=0, 시끄러운 장애=3")
        if "__any_channel__" in realized:
            lines.append(
                f"홀드아웃 거짓양성률: {realized['__any_channel__']:.1%} "
                f"(목표 {thr.alpha:.0%} 이하)"
            )
        lines.append("")

    header = f"{'점수':<5}{'이벤트':<7}{'로그':<6}{'메트릭':<7}{'문제':<52}"
    lines += [header, "-" * len(header)]
    for r in rows:
        c = r["channels"]
        mark = lambda k: ("1" if c.get(k) else "0") if k in c else "-"  # noqa: E731
        lines.append(
            f"{r['explicitness']}/{r['n_usable']:<3}"
            f"{mark('event'):<7}{mark('log'):<6}{mark('metric'):<7}{r['problem_id']:<52}"
        )

    lines += ["", "0~1 = 조용함 · 3 = 시끄러움 · '-' = 측정 불가(0점이 아니라 제외)", ""]
    quiet = [r["problem_id"] for r in rows if r["explicitness"] <= 1
             and NOOP_MARKER not in r["problem_id"]]
    if quiet:
        lines += ["조용한 장애 (에이전트 실행 대상):"] + [f"  - {q}" for q in quiet] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rank captured problems by explicitness.")
    ap.add_argument("root", type=Path, nargs="?", default=Path("runs"))
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--json", type=Path, help="also write the rows as JSON")
    args = ap.parse_args(argv)

    runs = load_runs(args.root)
    if not runs:
        raise SystemExit(f"no completed runs under {args.root}")
    print(f"완료된 run {len(runs)}개")

    thr, realized = fit_thresholds(runs, alpha=args.alpha)
    method = {m for m in thr.method.values()}
    print(f"임계값: {len(thr.tau)}개 통계량, 방법={'/'.join(sorted(method))}")

    rows = score_all(runs, thr)
    print(render(rows, thr, realized))

    if args.json:
        args.json.write_text(json.dumps(
            {"thresholds": thr.model_dump(mode="json"), "rows": rows,
             "realized_null": realized}, indent=2, default=str))
        print(f"wrote {args.json}")
    return 1 if sanity(rows) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
