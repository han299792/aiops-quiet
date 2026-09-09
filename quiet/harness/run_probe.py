"""Probe-only runs: capture a normal window and a fault window.

IMPURE. Runs on the lab machine. **No agent, no LLM, no cost.**

Deliberately does not use ``Orchestrator``. ``init_problem`` fuses
OpenEBS setup, Prometheus deploy, app delete, app deploy, fault injection
and workload start into one call with no hook between deploy and inject,
so there is nowhere to observe a steady state before the fault lands.
``start_problem`` then tears Prometheus down again on the way out, which
destroys the metric history spanning the normal-to-fault boundary. The
stock orchestrator cannot produce this experiment even in principle, so
this drives the problem object directly -- the same sequence, with the
hooks the measurement needs.

Everything captured is verbatim. Scoring happens later and offline, so a
scoring bug costs no cluster time.
"""

from __future__ import annotations

import argparse
import inspect
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..probe.collect import ClusterCollector, close_all, open_endpoints
from ..probe.parse import parse_window
from .rundir import RunDir, run_id


def _maybe_await(value):
    """`start_workload` is sync for some problems and async for others."""
    if inspect.isawaitable(value):
        import asyncio

        return asyncio.get_event_loop().run_until_complete(value)
    return value


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def ensure_env(kubectl) -> None:
    """Install OpenEBS and Prometheus ONCE per campaign, not per run.

    The orchestrator does this inside every ``init_problem`` and undoes it
    inside every ``start_problem``, including deleting the cluster's
    StorageClasses. Across a campaign that is both the dominant wall-clock
    cost and, on a shared cluster, repeated collateral damage: patching
    openebs-hostpath to be the default StorageClass changes provisioning
    for every workload that does not name one explicitly.
    """
    from aiopslab.service.telemetry.prometheus import Prometheus

    _log("ensuring OpenEBS...")
    kubectl.exec_command(
        "kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml"
    )
    kubectl.exec_command(
        "kubectl patch storageclass openebs-hostpath -p "
        "'{\"metadata\": {\"annotations\":"
        "{\"storageclass.kubernetes.io/is-default-class\":\"true\"}}}'"
    )
    kubectl.wait_for_ready("openebs")

    _log("ensuring Prometheus...")
    Prometheus().deploy()  # no-op when already running


def capture(
    problem_id: str,
    *,
    root: Path,
    replicate: int,
    warmup_s: int,
    window_s: int,
    settle_s: int,
    setup_env: bool = True,
) -> Path:
    # Imported here, not at module scope: importing aiopslab reads a
    # gitignored config.yml and loads a kubeconfig at import time, so a
    # top-level import would make this module unloadable on a laptop.
    from aiopslab.orchestrator.problems.registry import ProblemRegistry
    from aiopslab.service.kubectl import KubeCtl

    kubectl = KubeCtl()
    # get_problem returns the FACTORY; get_problem_instance calls it.
    problem = ProblemRegistry().get_problem_instance(problem_id)
    variant = getattr(problem, "variant", "n/a")

    rid = run_id(problem_id, replicate)
    rundir = RunDir.create(root, rid)
    _log(f"run {rid}")
    rundir.write(
        "spec.json",
        {
            "problem_id": problem_id,
            "variant": variant,
            "replicate": replicate,
            "warmup_s": warmup_s,
            "window_s": window_s,
            "settle_s": settle_s,
            "started_at": datetime.now(timezone.utc),
        },
    )

    if setup_env:
        ensure_env(kubectl)

    forwards = []
    status = "error"
    error: str | None = None
    try:
        _log("redeploying application...")
        problem.app.delete()
        problem.app.deploy()
        kubectl.wait_for_ready(problem.namespace)

        _maybe_await(problem.start_workload())

        endpoints, forwards = open_endpoints(problem.namespace)
        _log(f"endpoints: jaeger={endpoints.jaeger_base_url} "
             f"prom={endpoints.prometheus_base_url}")
        collector = ClusterCollector(problem.namespace, endpoints)

        # Warmup is discarded, never scored: the load generator ramps, and
        # a "normal" window taken during the ramp is not a steady state,
        # which shows up later as a held-out false-positive rate far above
        # alpha.
        _log(f"warmup {warmup_s}s (discarded)...")
        time.sleep(warmup_s)

        _log(f"normal window {window_s}s...")
        handle = collector.begin(
            run_id=rid, problem_id=problem_id, variant=variant, label="normal"
        )
        time.sleep(window_s)
        raw_normal = collector.end(handle)
        rundir.write("raw_normal.json", raw_normal)
        rundir.write("normal.json", parse_window(raw_normal))
        _log(f"  spans={len(raw_normal.spans)} logs={len(raw_normal.logs)} "
             f"events={len(raw_normal.events)} errors={raw_normal.collection_errors}")

        _log(f"injecting fault (dose={variant})...")
        problem.inject_fault()

        # flagd is rolled out on injection; the dose is not actually live
        # until that completes.
        _log(f"settle {settle_s}s...")
        time.sleep(settle_s)

        _log(f"fault window {window_s}s...")
        handle = collector.begin(
            run_id=rid, problem_id=problem_id, variant=variant, label="fault"
        )
        time.sleep(window_s)
        raw_fault = collector.end(handle)
        rundir.write("raw_fault.json", raw_fault)
        rundir.write("fault.json", parse_window(raw_fault))
        _log(f"  spans={len(raw_fault.spans)} logs={len(raw_fault.logs)} "
             f"events={len(raw_fault.events)} errors={raw_fault.collection_errors}")

        status = "ok"
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        error = repr(exc)
        raise
    finally:
        close_all(forwards)
        try:
            problem.recover_fault()
        except Exception as exc:  # noqa: BLE001
            _log(f"WARNING: recover_fault failed: {exc!r}")
        # run.json is the completion marker and must be written last.
        rundir.write(
            "run.json",
            {
                "run_id": rid,
                "problem_id": problem_id,
                "variant": variant,
                "status": status,
                "error": error,
                "finished_at": datetime.now(timezone.utc),
            },
        )
    return rundir.path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a normal and a fault window. No agent, no cost."
    )
    parser.add_argument("problem_id")
    parser.add_argument("--root", type=Path, default=Path("aiopslab-quiet/runs"))
    parser.add_argument("--replicate", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1,
                        help="consecutive replicates, numbered from --replicate")
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--window", type=int, default=600,
                        help="seconds per window; capture long, re-cut offline")
    parser.add_argument("--settle", type=int, default=90)
    parser.add_argument("--skip-env-setup", action="store_true",
                        help="skip OpenEBS/Prometheus setup (already done)")
    args = parser.parse_args(argv)

    failures = 0
    for i in range(args.repeat):
        replicate = args.replicate + i
        try:
            path = capture(
                args.problem_id,
                root=args.root,
                replicate=replicate,
                warmup_s=args.warmup,
                window_s=args.window,
                settle_s=args.settle,
                # Only the first replicate needs the cluster-wide setup.
                setup_env=(i == 0 and not args.skip_env_setup),
            )
            _log(f"wrote {path}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _log(f"replicate {replicate} FAILED: {exc!r}")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
