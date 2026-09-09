"""Data model for the signal-explicitness probe.

PURE MODULE. Must not import ``aiopslab`` or touch the network, the
filesystem or a cluster. See ``tests/test_import_purity.py``.

The pipeline has three artifacts, not two::

    RawWindow      verbatim payloads captured from the cluster
      -> parse ->  WindowSnapshot   compact statistics
      -> score ->  Verdict          per-channel 0/1 explicitness

Keeping ``RawWindow`` means one cluster session can capture ground truth
once; parsing and scoring are then developed and re-run offline forever.
"""

from __future__ import annotations

import bisect
import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Channel = Literal["event", "log", "metric"]
WindowLabel = Literal["normal", "fault"]


# --------------------------------------------------------------------------
# Latency binning
# --------------------------------------------------------------------------
def _log_edges(lo_ms: float, per_decade: int, count: int) -> tuple[float, ...]:
    return tuple(round(lo_ms * 10 ** (i / per_decade), 6) for i in range(count))


#: Fixed log-spaced bin edges in milliseconds, ~6 bins per decade from
#: 0.5 ms to ~73 s. Frozen as part of SCHEMA_VERSION: changing these
#: invalidates comparison against previously captured snapshots.
DUR_BIN_EDGES_MS: tuple[float, ...] = _log_edges(0.5, 6, 32)

#: Buckets = edges + 1. Bucket i holds values in [edges[i-1], edges[i]),
#: bucket 0 is (-inf, edges[0]) and the last is [edges[-1], +inf).
N_DUR_BUCKETS = len(DUR_BIN_EDGES_MS) + 1


def bucket_index(value_ms: float) -> int:
    """Bucket a duration. Inverse-ish of :func:`hist_quantile`."""
    return bisect.bisect_right(DUR_BIN_EDGES_MS, value_ms)


def make_hist(values_ms: list[float]) -> list[int]:
    hist = [0] * N_DUR_BUCKETS
    for v in values_ms:
        hist[bucket_index(v)] += 1
    return hist


def hist_total(hist: list[int]) -> int:
    return sum(hist)


def hist_quantile(hist: list[int], q: float) -> float | None:
    """Quantile of a binned distribution, interpolated within the bin.

    Returns ``None`` for an empty histogram. Values landing in the
    unbounded tail buckets are reported at the nearest finite edge, so a
    p99 that reads exactly ``DUR_BIN_EDGES_MS[-1]`` should be treated as
    bin-limited rather than exact.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    total = hist_total(hist)
    if total == 0:
        return None

    target = q * total
    cum = 0
    for i, count in enumerate(hist):
        if count == 0:
            continue
        if cum + count >= target:
            lo = DUR_BIN_EDGES_MS[i - 1] if i > 0 else 0.0
            hi = DUR_BIN_EDGES_MS[i] if i < len(DUR_BIN_EDGES_MS) else DUR_BIN_EDGES_MS[-1]
            frac = (target - cum) / count if count else 0.0
            return lo + (hi - lo) * min(max(frac, 0.0), 1.0)
        cum += count
    return DUR_BIN_EDGES_MS[-1]


# --------------------------------------------------------------------------
# Window identity
# --------------------------------------------------------------------------
class WindowSpec(BaseModel, frozen=True):
    """Identifies one observation window."""

    run_id: str
    problem_id: str
    #: Injected dose. "off" is the sham control; "none" means no injection
    #: happened at all (the noop problem); otherwise a flagd variant.
    variant: str
    label: WindowLabel
    namespace: str
    t_start: datetime
    t_end: datetime

    @property
    def minutes(self) -> float:
        seconds = (self.t_end - self.t_start).total_seconds()
        return seconds / 60.0


# --------------------------------------------------------------------------
# Per-channel statistics
# --------------------------------------------------------------------------
class EventStats(BaseModel):
    warning_count: int = 0
    normal_count: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)

    #: In-place container restarts, summed over containers present in BOTH
    #: the opening and closing pod census. Containers that only appear in
    #: one census are excluded: a rollout replaces pods rather than
    #: restarting containers, and counting the new pod's zero against
    #: nothing would read as churn, not as a restart.
    restart_delta: int = 0

    #: Pods that appeared or disappeared during the window. This is where
    #: a `kubectl rollout restart` shows up. The sham arm performs the same
    #: rollout, so calibration against sham subtracts it.
    pod_churn: int = 0

    pods_not_ready_at_end: int = 0


class LogStats(BaseModel):
    total_lines: int = 0
    error_lines: int = 0
    #: pod -> (error_lines, total_lines)
    per_pod: dict[str, tuple[int, int]] = Field(default_factory=dict)
    #: matcher name -> hits, for the qualitative appendix
    patterns: dict[str, int] = Field(default_factory=dict)
    #: True if any pod's log was cut off by --tail, which biases rates.
    truncated: bool = False
    samples: list[str] = Field(default_factory=list)


class ServiceRED(BaseModel):
    spans: int = 0
    error_spans: int = 0
    dur_hist_ms: list[int] = Field(default_factory=lambda: [0] * N_DUR_BUCKETS)

    def p(self, q: float) -> float | None:
        return hist_quantile(self.dur_hist_ms, q)


class TraceStats(BaseModel):
    per_service: dict[str, ServiceRED] = Field(default_factory=dict)
    total: ServiceRED = Field(default_factory=ServiceRED)


class SeriesStats(BaseModel):
    """Summary of one numeric time series, sufficient for a Welch t."""

    mean: float
    var: float
    n: int


class ResourceStats(BaseModel):
    #: "<pod>|<metric>" -> SeriesStats
    series: dict[str, SeriesStats] = Field(default_factory=dict)


class SpanRecord(BaseModel):
    """One span, kept with its start time so windows can be re-cut.

    Storing spans individually rather than pre-binned is what makes the
    offline window-length sweep possible: explicitness is a function of
    fault size TIMES observation volume, so a single long capture can be
    sliced into shorter windows afterwards at no cluster cost. A
    histogram cannot be re-cut.
    """

    service: str
    operation: str = ""
    start_ms: float
    duration_ms: float
    has_error: bool = False


class RawLogLine(BaseModel):
    pod: str
    #: RFC3339 timestamp as emitted by `kubectl logs --timestamps`, or
    #: empty when the line carried none.
    ts: str = ""
    text: str


class RawWindow(BaseModel):
    """Verbatim capture, before any statistics are taken.

    Written to disk alongside every run and NOT summarised on the way in.
    One cluster session captures this once; parsing and scoring are then
    developed and re-run on a laptop indefinitely, and a parser fix can
    be applied retroactively to every historical run.
    """

    schema_version: int = SCHEMA_VERSION
    spec: WindowSpec
    #: `kubectl get events -o json` items, unmodified.
    events: list[dict] = Field(default_factory=list)
    #: Pod census at window open and close: pod -> container -> restartCount
    pods_at_start: dict[str, dict[str, int]] = Field(default_factory=dict)
    pods_at_end: dict[str, dict[str, int]] = Field(default_factory=dict)
    #: Pods not Ready at window close.
    not_ready_at_end: list[str] = Field(default_factory=list)
    logs: list[RawLogLine] = Field(default_factory=list)
    #: Pods whose logs hit the --tail cap, so their rates are biased.
    truncated_pods: list[str] = Field(default_factory=list)
    spans: list[SpanRecord] = Field(default_factory=list)
    #: "<pod>|<metric>" -> [(unix_seconds, value), ...]
    resource_series: dict[str, list[tuple[float, float]]] = Field(default_factory=dict)
    collection_errors: list[str] = Field(default_factory=list)


class WindowSnapshot(BaseModel):
    schema_version: int = SCHEMA_VERSION
    spec: WindowSpec
    events: EventStats | None = None
    logs: LogStats | None = None
    traces: TraceStats | None = None
    resources: ResourceStats | None = None
    #: One entry per source that failed. A dead source degrades its
    #: channel to unusable; it never aborts the run.
    collection_errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Scoring artifacts
# --------------------------------------------------------------------------
#: Sub-statistic keys, grouped by channel. Thresholds are calibrated per
#: sub-statistic and a channel fires if ANY of its usable sub-statistics
#: exceeds threshold. Calibrating per sub-statistic (rather than taking a
#: max over mixed scales) keeps each null interpretable and makes it
#: visible which signal actually fired.
SUBSTATS: dict[Channel, tuple[str, ...]] = {
    "event": ("event.warn_rate_z", "event.restart_delta", "event.pod_churn"),
    "log": ("log.error_rate_z",),
    "metric": ("metric.trace_error_z", "metric.latency_bm_z", "metric.resource_t"),
}

ALL_SUBSTATS: tuple[str, ...] = tuple(k for keys in SUBSTATS.values() for k in keys)

SUBSTAT_CHANNEL: dict[str, Channel] = {
    key: channel for channel, keys in SUBSTATS.items() for key in keys
}


class SubEffect(BaseModel):
    """One signed, higher-is-more-anomalous statistic for one window pair."""

    key: str
    channel: Channel
    statistic: float
    #: False when either window lacked the data to compute it. Unusable
    #: sub-statistics are EXCLUDED from scoring, never scored as zero --
    #: an absent measurement is not evidence of absence.
    usable: bool = True
    note: str = ""
    detail: dict[str, float] = Field(default_factory=dict)


class Thresholds(BaseModel):
    schema_version: int = SCHEMA_VERSION
    #: sub-statistic key -> threshold, or None when the statistic was
    #: never measurable in the null and therefore cannot fire.
    #:
    #: None rather than +inf on purpose: JSON has no infinity, so an inf
    #: sentinel silently round-trips to null through thresholds.json and
    #: then blows up (or worse, compares wrong) on load. An explicit
    #: optional survives serialization and forces callers to handle it.
    tau: dict[str, float | None]
    method: dict[str, Literal["empirical_quantile", "mad_fallback"]]
    alpha: float
    #: Family-wise correction denominator actually used per key.
    correction: dict[str, int]
    n_null: dict[str, int]
    fitted_at: datetime
    #: min / median / mad / max of the null, for the write-up
    null_summary: dict[str, dict[str, float]] = Field(default_factory=dict)


class Verdict(BaseModel):
    per_channel: dict[str, int] = Field(default_factory=dict)
    unusable_channels: list[str] = Field(default_factory=list)
    #: Sum of fired channels over the USABLE channels only.
    explicitness: int = 0
    n_usable_channels: int = 0
    #: explicitness / n_usable_channels, or None when nothing was usable.
    explicitness_frac: float | None = None
    #: sub-statistic key -> (statistic - tau). The continuous version:
    #: regress THIS against dose, not the saturating integer.
    margins: dict[str, float] = Field(default_factory=dict)
    fired: list[str] = Field(default_factory=list)


def is_finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)
