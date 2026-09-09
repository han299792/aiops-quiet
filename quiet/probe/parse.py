"""RawWindow -> WindowSnapshot.

PURE MODULE. This is where every real-payload quirk gets absorbed, and
it is deliberately separated from collection so that a parser fix can be
replayed over every stored RawWindow without going near a cluster.

Two quirks are handled here that would otherwise fail silently:

* **Event timestamps.** A naive filter on ``lastTimestamp`` returns zero
  events on any modern cluster, because ``events.k8s.io`` populates
  ``eventTime`` / ``series.lastObservedTime`` instead. Zero events is not
  an error -- it reads as "the event channel saw nothing", which is
  exactly what a quiet failure looks like. The bug would masquerade as
  the result.
* **Restart deltas across a rollout.** ``kubectl rollout restart`` replaces
  pods rather than restarting containers, so the new pod's restartCount
  starts at 0. Differencing over the union of pods would count that as
  churn; only containers present in BOTH censuses are differenced, and
  pod replacement is reported separately as ``pod_churn``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .model import (
    EventStats,
    LogStats,
    RawWindow,
    ResourceStats,
    ServiceRED,
    SeriesStats,
    SpanRecord,
    TraceStats,
    WindowSnapshot,
    WindowSpec,
    make_hist,
)

#: Matched case-insensitively against each log line. A line counts once,
#: under the FIRST rule that matches, so the specific patterns must come
#: before the generic one -- "rpc error: code = Unavailable" contains the
#: word "error", and with the generic rule first the specific rules were
#: unreachable for every line that mattered.
#:
#: Only the total (error_lines) feeds scoring; this breakdown is
#: diagnostic, which is exactly why a wrong order here would have gone
#: unnoticed while quietly making the breakdown useless.
ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("fatal", r"\bfatal\b|\bpanic\b"),
    ("grpc_error", r"\brpc error\b|\bcode = (Internal|Unavailable|Unknown|DeadlineExceeded)\b"),
    ("http_5xx", r'"?status(_code)?"?\s*[:=]\s*"?5\d{2}\b|\bHTTP/\d\.\d"?\s+5\d{2}\b'),
    ("error", r"\berror\b|\berr\b|\bexception\b|\bfailed\b|\bfailure\b"),
)

_COMPILED = tuple((name, re.compile(pat, re.IGNORECASE)) for name, pat in ERROR_PATTERNS)

MAX_LOG_SAMPLES = 25

_EVENT_TIME_FIELDS = (
    "lastTimestamp",       # core/v1
    "eventTime",           # events.k8s.io
    "firstTimestamp",
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def event_time(event: dict) -> datetime | None:
    """Best available timestamp, across both event API shapes."""
    series = event.get("series") or {}
    candidates = [series.get("lastObservedTime")] + [
        event.get(field) for field in _EVENT_TIME_FIELDS
    ]
    # metadata.creationTimestamp is the last resort: always present.
    candidates.append((event.get("metadata") or {}).get("creationTimestamp"))
    for candidate in candidates:
        parsed = _parse_ts(candidate)
        if parsed is not None:
            return parsed
    return None


def classify_line(text: str) -> str | None:
    for name, pattern in _COMPILED:
        if pattern.search(text):
            return name
    return None


def parse_events(raw: RawWindow) -> EventStats:
    spec = raw.spec
    warning = 0
    normal = 0
    reasons: dict[str, int] = {}

    for event in raw.events:
        when = event_time(event)
        if when is not None and not (spec.t_start <= when <= spec.t_end):
            continue
        # An event object carries a count for repeats; honour it so a
        # tight crash loop is not scored the same as a single blip.
        count = int(event.get("count") or (event.get("series") or {}).get("count") or 1)
        if str(event.get("type", "")).lower() == "warning":
            warning += count
        else:
            normal += count
        reason = str(event.get("reason", "") or "Unknown")
        reasons[reason] = reasons.get(reason, 0) + count

    # Only containers observed at BOTH ends are differenced.
    restart_delta = 0
    for pod, containers in raw.pods_at_end.items():
        before = raw.pods_at_start.get(pod)
        if before is None:
            continue
        for container, count in containers.items():
            if container in before:
                restart_delta += max(0, count - before[container])

    start_pods = set(raw.pods_at_start)
    end_pods = set(raw.pods_at_end)
    pod_churn = len(start_pods ^ end_pods)

    return EventStats(
        warning_count=warning,
        normal_count=normal,
        reasons=reasons,
        restart_delta=restart_delta,
        pod_churn=pod_churn,
        pods_not_ready_at_end=len(raw.not_ready_at_end),
    )


def parse_logs(raw: RawWindow) -> LogStats:
    per_pod: dict[str, tuple[int, int]] = {}
    patterns: dict[str, int] = {}
    samples: list[str] = []
    total = 0
    errors = 0

    for line in raw.logs:
        when = _parse_ts(line.ts)
        if when is not None and not (raw.spec.t_start <= when <= raw.spec.t_end):
            continue
        pod_errors, pod_total = per_pod.get(line.pod, (0, 0))
        pod_total += 1
        total += 1
        kind = classify_line(line.text)
        if kind is not None:
            pod_errors += 1
            errors += 1
            patterns[kind] = patterns.get(kind, 0) + 1
            if len(samples) < MAX_LOG_SAMPLES:
                samples.append(f"[{line.pod}] {line.text[:300]}")
        per_pod[line.pod] = (pod_errors, pod_total)

    return LogStats(
        total_lines=total,
        error_lines=errors,
        per_pod=per_pod,
        patterns=patterns,
        truncated=bool(raw.truncated_pods),
        samples=samples,
    )


def parse_traces(raw: RawWindow, *, spec: WindowSpec | None = None) -> TraceStats:
    """Bin spans by service.

    ``spec`` overrides the window bounds, which is how the offline
    window-length sweep re-cuts one long capture into shorter windows.
    """
    bounds = spec or raw.spec
    start_ms = bounds.t_start.timestamp() * 1000.0
    end_ms = bounds.t_end.timestamp() * 1000.0

    grouped: dict[str, list[SpanRecord]] = {}
    for span in raw.spans:
        if not (start_ms <= span.start_ms <= end_ms):
            continue
        grouped.setdefault(span.service, []).append(span)

    per_service: dict[str, ServiceRED] = {}
    for service, spans in grouped.items():
        per_service[service] = ServiceRED(
            spans=len(spans),
            error_spans=sum(1 for s in spans if s.has_error),
            dur_hist_ms=make_hist([s.duration_ms for s in spans]),
        )

    all_spans = [s for spans in grouped.values() for s in spans]
    total = ServiceRED(
        spans=len(all_spans),
        error_spans=sum(1 for s in all_spans if s.has_error),
        dur_hist_ms=make_hist([s.duration_ms for s in all_spans]),
    )
    return TraceStats(per_service=per_service, total=total)


def parse_resources(raw: RawWindow, *, spec: WindowSpec | None = None) -> ResourceStats:
    bounds = spec or raw.spec
    lo = bounds.t_start.timestamp()
    hi = bounds.t_end.timestamp()

    series: dict[str, SeriesStats] = {}
    for name, points in raw.resource_series.items():
        values = [v for ts, v in points if lo <= ts <= hi]
        n = len(values)
        if n == 0:
            continue
        mean = sum(values) / n
        # Sample variance; 0 for a single point, which welch_t rejects.
        var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
        series[name] = SeriesStats(mean=mean, var=var, n=n)
    return ResourceStats(series=series)


def parse_window(raw: RawWindow, *, spec: WindowSpec | None = None) -> WindowSnapshot:
    """Summarise a raw capture.

    Pass ``spec`` with narrower bounds to re-cut the capture into a
    shorter window; everything is filtered against it consistently.
    """
    bounds = spec or raw.spec
    if bounds.t_end <= bounds.t_start:
        raise ValueError(f"window must have positive duration: {bounds}")

    narrowed = raw if spec is None else raw.model_copy(update={"spec": bounds})
    return WindowSnapshot(
        spec=bounds,
        events=parse_events(narrowed),
        logs=parse_logs(narrowed),
        traces=parse_traces(raw, spec=bounds),
        resources=parse_resources(raw, spec=bounds),
        collection_errors=list(raw.collection_errors),
    )


def subwindows(raw: RawWindow, *, minutes: float, count: int) -> list[WindowSnapshot]:
    """Re-cut one capture into ``count`` consecutive windows of ``minutes``.

    The offline half of the window-length axis: no cluster time, and the
    only reason RawWindow keeps per-span timestamps.
    """
    from datetime import timedelta

    if minutes <= 0 or count <= 0:
        raise ValueError("minutes and count must be positive")
    span = timedelta(minutes=minutes)
    if raw.spec.t_start + span * count > raw.spec.t_end:
        raise ValueError(
            f"capture is {raw.spec.minutes:.1f} min, too short for "
            f"{count} x {minutes} min"
        )
    out = []
    for i in range(count):
        start = raw.spec.t_start + span * i
        out.append(
            parse_window(
                raw, spec=raw.spec.model_copy(update={"t_start": start, "t_end": start + span})
            )
        )
    return out
