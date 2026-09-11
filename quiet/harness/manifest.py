"""Reproducibility fingerprint for a run.

Rule: **a run without a manifest is a log, not a result.** It does not go
into the catalogue. Everything here answers "what produced this number",
because a number whose environment cannot be named is not reusable three
weeks later — and quiet failures are subtle enough that a slightly
different application version stops being quiet.

Impure only in that it shells out to git/kubectl; it never needs the
cluster to be healthy, so it is safe to call even on a run that failed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..paths import aiopslab_root


def _sh(cmd: str, cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=60, cwd=cwd
        )
        return p.stdout.strip() or f"[rc={p.returncode}] {p.stderr.strip()[:200]}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc!r}"


def sha256_of(path: Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def image_digests(namespace: str) -> list[str]:
    """Container image digests, not tags.

    A `latest` tag resolves to a different binary later; a digest does
    not. This is the field that makes "same app" checkable.
    """
    out = _sh(
        "kubectl get pods -n %s -o "
        "jsonpath='{range .items[*]}{range .status.containerStatuses[*]}"
        "{.imageID}{\"\\n\"}{end}{end}'" % namespace
    )
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def build(
    *,
    problem_id: str,
    namespace: str,
    warmup_s: int,
    window_s: int,
    settle_s: int,
    extra: dict | None = None,
) -> dict:
    repo = Path(__file__).resolve().parents[2]
    ail = aiopslab_root()

    thresholds = repo / "env" / "thresholds.json"
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "problem_id": problem_id,
        "namespace": namespace,
        # --- code ---
        "quiet_sha": _sh("git rev-parse HEAD", cwd=repo),
        "quiet_dirty": bool(_sh("git status --porcelain", cwd=repo)),
        "aiopslab_sha": _sh("git rev-parse HEAD", cwd=ail),
        "aiopslab_dirty": bool(_sh("git status --porcelain", cwd=ail)),
        "aiopslab_submodules": _sh("git submodule status", cwd=ail),
        # --- environment ---
        "k8s_version": _sh("kubectl version -o json"),
        "helm_releases": _sh("helm list -A -o json"),
        "image_digests": image_digests(namespace),
        # --- experiment parameters ---
        "warmup_s": warmup_s,
        "window_s": window_s,
        "settle_s": settle_s,
        "schema_version": _schema_version(),
        # Calibrated thresholds are part of the result: the same snapshots
        # scored against different thresholds are different numbers.
        "thresholds_sha256": sha256_of(thresholds),
        **(extra or {}),
    }


def _schema_version() -> int:
    from ..probe.model import SCHEMA_VERSION

    return SCHEMA_VERSION


def write(rundir, **kwargs) -> None:
    rundir.write("manifest.json", build(**kwargs))


def is_complete(manifest: dict) -> tuple[bool, list[str]]:
    """Fields without which a run cannot be compared to another."""
    required = ["quiet_sha", "aiopslab_sha", "k8s_version", "window_s", "schema_version"]
    missing = [k for k in required if not manifest.get(k)]
    if manifest.get("quiet_dirty") or manifest.get("aiopslab_dirty"):
        missing.append("clean worktree (code was uncommitted at run time)")
    return (not missing), missing
