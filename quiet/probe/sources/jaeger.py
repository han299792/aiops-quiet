"""Jaeger trace source, addressed by absolute time.

IMPURE. Requires a cluster.

Not built on ``aiopslab.observer.trace_api``, because that cannot express
what this experiment needs: ``get_traces`` accepts ``end_time`` and drops
it, computing ``lookback = now - start_time`` instead. A window that has
already ended is unreachable, which rules out any normal-versus-fault
comparison. The Jaeger HTTP API itself takes absolute ``start`` and
``end`` in microseconds, which is what this uses.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime

from ..model import SpanRecord

#: Traces per service per window. Measured on the cluster: 200 traces of
#: frontend-proxy is already ~2 MB and 2,000 is ~14 MB, so a large limit
#: across twenty services drops the connection mid-response. Lower is not a
#: loss of data so much as a bound on how much of a busy window one request
#: tries to carry; fetch_spans reports when the cap is reached so a biased
#: rate is visible rather than silent.
DEFAULT_LIMIT = 400

_ERROR_TAGS = {"error", "otel.status_code"}


class JaegerError(RuntimeError):
    pass


def _get(base_url: str, path: str, params: dict[str, str | int],
         timeout: int = 120, retries: int = 2):
    """GET with retries.

    A large trace response can drop the connection part-way
    (RemoteDisconnected) even when the query is valid, so a transient
    failure is retried before it is reported as missing data -- otherwise
    the channel degrades to unusable for reasons that have nothing to do
    with the telemetry.
    """
    url = f"{base_url}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.0 + attempt)
    raise JaegerError(f"GET {url} failed after {retries + 1} tries: {last!r}") from last


#: Jaeger's API is not always at the root. The OpenTelemetry demo serves the
#: UI and its API under /jaeger/ui, and the bare root returns the HTML app --
#: which answers 200, so a status-code health check passes while every API
#: call comes back as HTML.
API_PREFIXES = ("", "/jaeger/ui", "/jaeger")


def resolve_base(base_url: str) -> str:
    """Return base_url with whatever prefix actually serves the JSON API."""
    for prefix in API_PREFIXES:
        try:
            data = _get(base_url + prefix, "/api/services", {})
        except JaegerError:
            continue
        if isinstance(data, dict) and "data" in data:
            return base_url + prefix
    raise JaegerError(
        f"no Jaeger JSON API under {base_url} (tried {list(API_PREFIXES)}); "
        "the root may be serving the UI"
    )


def list_services(base_url: str) -> list[str]:
    data = _get(base_url, "/api/services", {})
    return [s for s in (data.get("data") or []) if s and s != "jaeger-all-in-one"]


def dedupe_spans(spans: list[SpanRecord]) -> list[SpanRecord]:
    """Drop spans seen more than once.

    Jaeger returns a whole trace for every service it touches, so
    querying service by service yields the same span several times.
    Left in, a shared span would be counted once per participating
    service and every error RATE would be wrong -- not by a constant
    factor either, since fan-out differs per service.

    Pure, so it is unit-tested; the surrounding fetch is not.
    """
    seen: set[tuple] = set()
    unique: list[SpanRecord] = []
    for span in spans:
        key = (span.service, span.operation, span.start_ms, span.duration_ms)
        if key in seen:
            continue
        seen.add(key)
        unique.append(span)
    return unique


def span_has_error(span: dict) -> bool:
    for tag in span.get("tags") or []:
        key = tag.get("key")
        value = tag.get("value")
        if key == "error" and value in (True, "true", "True"):
            return True
        if key == "otel.status_code" and str(value).upper() == "ERROR":
            return True
        if key in ("http.status_code", "http.response.status_code"):
            try:
                if int(value) >= 500:
                    return True
            except (TypeError, ValueError):
                pass
        if key in ("rpc.grpc.status_code", "grpc.status_code"):
            try:
                if int(value) != 0:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def fetch_spans(
    base_url: str,
    start: datetime,
    end: datetime,
    *,
    services: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[SpanRecord], list[str]]:
    """Every span in ``[start, end]``, flattened. Returns (spans, errors).

    Per-span records rather than aggregates, because a capture has to
    stay re-cuttable into shorter windows offline.
    """
    names = services if services is not None else list_services(base_url)
    start_us = int(start.timestamp() * 1_000_000)
    end_us = int(end.timestamp() * 1_000_000)

    out: list[SpanRecord] = []
    problems: list[str] = []
    for service in names:
        try:
            data = _get(
                base_url,
                "/api/traces",
                {"service": service, "start": start_us, "end": end_us, "limit": limit},
            )
        except JaegerError as exc:
            problems.append(str(exc))
            continue

        for trace in data.get("data") or []:
            # process id -> service name, since a trace spans services.
            processes = {
                pid: (proc or {}).get("serviceName", service)
                for pid, proc in (trace.get("processes") or {}).items()
            }
            for span in trace.get("spans") or []:
                out.append(
                    SpanRecord(
                        service=processes.get(span.get("processID"), service),
                        operation=span.get("operationName", ""),
                        start_ms=float(span.get("startTime", 0)) / 1000.0,
                        duration_ms=float(span.get("duration", 0)) / 1000.0,
                        has_error=span_has_error(span),
                    )
                )

    unique = dedupe_spans(out)

    if len(unique) >= limit:
        problems.append(
            f"span limit {limit} reached; rates may be biased by truncation"
        )
    return unique, problems
