"""Score a set of existing problems and rank them by explicitness.

IMPURE. Runs on the cluster. **No agent, no LLM, no cost.**

This is the deliverable: a table of the benchmark's own problems ordered
by how explicitly each fault is written into telemetry. It needs no new
faults -- the registry already ships 40-plus, and which of them are quiet
is decided by the measurement rather than chosen in advance.

Two arms carry the method:

* ``noop`` -- no fault is injected at all, so every channel should read
  zero. Its runs supply the null distribution the thresholds come from,
  and any signal it shows is the meter's own noise.
* ``pod_kill`` -- a loud fault. If it does not score 3, the meter is
  broken and nothing below it can be trusted.

Usage::

    python -m quiet.harness.run_catalog --calibrate-runs 5
    python -m quiet.analysis.catalog runs/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .run_probe import _log, capture

#: Hotel-reservation problems. Chaos Mesh and application-level faults.
HOTEL = [
    "noop_detection_hotel_reservation-1",
    "pod_kill_hotel_res-detection-1",
    "network_delay_hotel_res-detection-1",
    "misconfig_app_hotel_res-detection-1",
    "revoke_auth_mongodb-detection-1",
]

#: Astronomy-shop problems. Feature-flag faults; pods stay Ready, so only
#: telemetry reveals them -- the interesting end of the range.
ASTRONOMY = [
    "noop_detection_astronomy_shop-1",
    "astronomy_shop_image_slow_load-detection-1",
    "astronomy_shop_kafka_queue_problems-detection-1",
    "astronomy_shop_recommendation_service_cache_failure-detection-1",
    "astronomy_shop_ad_service_manual_gc-detection-1",
    "astronomy_shop_payment_service_failure-detection-1",
]

DEFAULT = HOTEL + ASTRONOMY

#: Run these repeatedly first: they define the thresholds everything else
#: is scored against.
CALIBRATION = [
    "noop_detection_hotel_reservation-1",
    "noop_detection_astronomy_shop-1",
]


def run_many(
    problem_ids: list[str],
    *,
    root: Path,
    warmup_s: int,
    window_s: int,
    settle_s: int,
    replicate: int = 1,
    first_sets_up_env: bool = True,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Capture each problem in turn. One failure does not stop the rest."""
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    for i, pid in enumerate(problem_ids):
        _log(f"--- [{i + 1}/{len(problem_ids)}] {pid} ---")
        try:
            path = capture(
                pid,
                root=root,
                replicate=replicate,
                warmup_s=warmup_s,
                window_s=window_s,
                settle_s=settle_s,
                setup_env=(i == 0 and first_sets_up_env),
            )
            done.append(pid)
            _log(f"    wrote {path}")
        except Exception as exc:  # noqa: BLE001
            # A problem that will not deploy is a finding, not a reason to
            # abandon the other seven.
            failed.append((pid, repr(exc)))
            _log(f"    FAILED: {exc!r}")
    return done, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Capture normal/fault windows for a set of problems. No agent."
    )
    ap.add_argument("--root", type=Path, default=Path("runs"))
    ap.add_argument("--problems", nargs="*", default=None,
                    help="default: the built-in catalogue set")
    ap.add_argument("--app", choices=["hotel", "astronomy", "both"], default="both")
    ap.add_argument("--calibrate-runs", type=int, default=5,
                    help="repeats of the noop arms, which set the thresholds")
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--window", type=int, default=600)
    ap.add_argument("--settle", type=int, default=90)
    args = ap.parse_args(argv)

    if args.problems:
        problems = args.problems
    elif args.app == "hotel":
        problems = HOTEL
    elif args.app == "astronomy":
        problems = ASTRONOMY
    else:
        problems = DEFAULT

    kw = dict(root=args.root, warmup_s=args.warmup,
              window_s=args.window, settle_s=args.settle)

    # 1. Calibration first. Thresholds have to exist before anything is
    #    scored, and deriving them from runs that are already in hand
    #    would be choosing the threshold after seeing the result.
    if args.calibrate_runs > 0:
        cal = [p for p in CALIBRATION if p in problems or not args.problems]
        _log(f"=== calibration: {len(cal)} noop arms x {args.calibrate_runs} ===")
        for r in range(1, args.calibrate_runs + 1):
            run_many(cal, replicate=r, first_sets_up_env=(r == 1), **kw)

    # 2. Then the rest, once each.
    rest = [p for p in problems if p not in CALIBRATION]
    _log(f"=== catalogue: {len(rest)} problems ===")
    done, failed = run_many(rest, replicate=1, first_sets_up_env=False, **kw)

    _log(f"captured {len(done)}, failed {len(failed)}")
    for pid, err in failed:
        _log(f"  FAILED {pid}: {err[:160]}")
    _log("next: python -m quiet.analysis.catalog " + str(args.root))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
