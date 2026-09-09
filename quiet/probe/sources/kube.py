"""kubectl-backed sources: events, the pod census, and logs.

IMPURE. Requires a cluster and a kubeconfig.

Deliberately shells out to kubectl instead of using the Python client, so
the collector inherits nothing from ``aiopslab.observer`` -- which loads
a kubeconfig at import time -- and the pure layer stays importable on a
laptop.

Logs come from ``kubectl logs`` rather than the Filebeat -> Logstash ->
Elasticsearch path, which needs an Elasticsearch outside the cluster,
hardcodes its address, and targets the wrong namespace by default. Not
using it also means no node-wide DaemonSet, which keeps the collector
from reading workloads that are not ours.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime

from ..model import RawLogLine

#: Per pod. A pod that hits this has a biased error rate, so it is
#: reported via `truncated_pods` rather than silently used.
DEFAULT_TAIL = 20000


class KubectlError(RuntimeError):
    pass


def _run(args: list[str], *, timeout: int = 120) -> str:
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise KubectlError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def get_events(namespace: str) -> list[dict]:
    """Raw event items, unfiltered. Windowing happens in parse.py."""
    out = _run(["kubectl", "get", "events", "-n", namespace, "-o", "json"])
    return json.loads(out).get("items", [])


def pod_census(namespace: str) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Restart counts per container, plus the pods that are not Ready.

    Returns ``({pod: {container: restartCount}}, [not_ready_pods])``.
    Taken at both window edges so restarts can be differenced over the
    containers that exist at both ends -- a rollout replaces pods, and
    differencing over the union would score the replacement as churn.
    """
    out = _run(["kubectl", "get", "pods", "-n", namespace, "-o", "json"])
    census: dict[str, dict[str, int]] = {}
    not_ready: list[str] = []

    for item in json.loads(out).get("items", []):
        name = item["metadata"]["name"]
        status = item.get("status") or {}
        containers = {
            cs["name"]: int(cs.get("restartCount", 0))
            for cs in status.get("containerStatuses") or []
        }
        census[name] = containers
        ready = all(
            cs.get("ready", False) for cs in status.get("containerStatuses") or []
        )
        if not ready or status.get("phase") not in ("Running", "Succeeded"):
            not_ready.append(name)
    return census, not_ready


def list_pod_names(namespace: str) -> list[str]:
    out = _run(
        ["kubectl", "get", "pods", "-n", namespace,
         "-o", "jsonpath={.items[*].metadata.name}"]
    )
    return out.split()


def get_logs(
    namespace: str,
    since: datetime,
    *,
    pods: list[str] | None = None,
    tail: int = DEFAULT_TAIL,
) -> tuple[list[RawLogLine], list[str]]:
    """Timestamped log lines since ``since``, plus the pods that truncated.

    A pod whose logs fail to fetch (terminating, no containers yet) is
    skipped rather than aborting the window -- one dead pod must not cost
    the whole run.
    """
    names = pods if pods is not None else list_pod_names(namespace)
    since_arg = since.astimezone().isoformat(timespec="seconds")

    lines: list[RawLogLine] = []
    truncated: list[str] = []
    for pod in names:
        try:
            out = _run(
                ["kubectl", "logs", pod, "-n", namespace, "--all-containers",
                 "--timestamps", f"--since-time={since_arg}", f"--tail={tail}"]
            )
        except (KubectlError, subprocess.TimeoutExpired):
            continue
        pod_lines = [ln for ln in out.splitlines() if ln.strip()]
        if len(pod_lines) >= tail:
            truncated.append(pod)
        for raw in pod_lines:
            ts, _, text = raw.partition(" ")
            # kubectl prefixes an RFC3339 timestamp; if the split does not
            # look like one, keep the whole line as text.
            if len(ts) >= 20 and ts[4] == "-":
                lines.append(RawLogLine(pod=pod, ts=ts, text=text))
            else:
                lines.append(RawLogLine(pod=pod, ts="", text=raw))
    return lines, truncated
