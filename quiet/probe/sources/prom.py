"""Prometheus range queries, direct.

IMPURE. Requires a cluster.

Not built on ``aiopslab.observer.metric_api.PrometheusAPI``, which picks
a free local port in 32000-32100, forwards to it, and then queries
whatever URL the caller passed -- and every caller passes a hardcoded
``http://localhost:32000``. If 32000 is already taken the two diverge
silently. It also returns a dict on no-data where callers expect a list,
and stamps values in Asia/Shanghai.

Talking to ``/api/v1/query_range`` is about forty lines and avoids all
three.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime

#: cAdvisor only: the vendored Prometheus scrapes cAdvisor and
#: node-exporter and nothing else. There are no RED metrics here, which
#: is why latency and error rate come from traces instead.
DEFAULT_QUERIES: dict[str, str] = {
    "cpu": 'rate(container_cpu_usage_seconds_total{{namespace="{ns}",container!="",pod!=""}}[2m])',
    "mem": 'container_memory_working_set_bytes{{namespace="{ns}",container!="",pod!=""}}',
}


class PrometheusError(RuntimeError):
    pass


def query_range(
    base_url: str,
    query: str,
    start: datetime,
    end: datetime,
    *,
    step: int = 60,
    timeout: int = 60,
) -> list[dict]:
    params = {
        "query": query,
        "start": str(start.timestamp()),
        "end": str(end.timestamp()),
        "step": str(step),
    }
    url = f"{base_url}/api/v1/query_range?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        raise PrometheusError(f"query_range failed: {exc!r}") from exc

    if payload.get("status") != "success":
        raise PrometheusError(f"query failed: {payload.get('error', payload)}")
    return payload.get("data", {}).get("result", [])


def fetch_series(
    base_url: str,
    namespace: str,
    start: datetime,
    end: datetime,
    *,
    step: int = 60,
    queries: dict[str, str] | None = None,
) -> tuple[dict[str, list[tuple[float, float]]], list[str]]:
    """``{"<pod>|<metric>": [(unix_seconds, value), ...]}`` plus errors.

    ``step`` should not be finer than the scrape interval (1m in the
    vendored chart) or Prometheus interpolates and the extra points are
    not independent observations -- which would make the Welch t on this
    channel overconfident.
    """
    queries = queries or DEFAULT_QUERIES
    out: dict[str, list[tuple[float, float]]] = {}
    problems: list[str] = []

    for metric, template in queries.items():
        try:
            results = query_range(
                base_url, template.format(ns=namespace), start, end, step=step
            )
        except PrometheusError as exc:
            problems.append(str(exc))
            continue
        for series in results:
            pod = (series.get("metric") or {}).get("pod")
            if not pod:
                continue
            key = f"{pod}|{metric}"
            points = out.setdefault(key, [])
            for ts, value in series.get("values") or []:
                try:
                    points.append((float(ts), float(value)))
                except (TypeError, ValueError):
                    continue
    return out, problems
