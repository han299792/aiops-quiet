"""Turn a (normal, fault) snapshot pair into per-channel explicitness.

PURE MODULE. No cluster, no network, no filesystem.

Design notes that matter for reading the numbers later:

*Maxima and multiplicity.* Several sub-statistics are a maximum over
entities (pods, services, series). That is deliberate: the faults under
study are localized -- ``paymentFailure`` hits one service -- so a
namespace-wide aggregate would dilute a real 10% effect into nothing.
The multiplicity is not ignored, it is absorbed: thresholds are
calibrated on the null distribution OF THE SAME MAXIMUM, computed over
the same entity set. This only holds while the entity set is stable, so
:func:`compute_effects` records the entity count it maximized over.

*Orientation under swap.* The plain difference statistics (restart
delta, pod churn) negate when the windows are swapped. The
max-over-entities ones do NOT: swapping turns ``max_i z_i`` into
``-min_i z_i``, so their null sits to the right of zero. That is fine
and expected -- the threshold is calibrated on the null of the same
maximum -- but it means a null distribution centred away from zero is
not evidence of a bug.

*Unusable is not zero.* When a channel could not be measured it is
excluded from the denominator rather than scored 0. An absent
measurement is not evidence of absence.
"""

from __future__ import annotations

from .model import (
    ALL_SUBSTATS,
    SUBSTAT_CHANNEL,
    SUBSTATS,
    Channel,
    SubEffect,
    Thresholds,
    Verdict,
    WindowSnapshot,
)
from .stats import brunner_munzel_z_from_hist, rate_ratio_z, two_proportion_z, welch_t

#: A resource series needs at least this many scrapes in BOTH windows to
#: be scored. Prometheus scrapes at 1m by default, so a 5-minute window
#: yields ~5 points and this channel is underpowered by construction --
#: which is a finding, not a bug, and is why it degrades to unusable
#: rather than to a zero that would silently drag the score down.
MIN_RESOURCE_SAMPLES = 3


def _unusable(key: str, note: str) -> SubEffect:
    return SubEffect(
        key=key,
        channel=SUBSTAT_CHANNEL[key],
        statistic=0.0,
        usable=False,
        note=note,
    )


def _max_over(
    candidates: dict[str, float],
) -> tuple[float, str] | None:
    """Argmax helper. Returns (value, entity) or None if nothing scored."""
    if not candidates:
        return None
    entity = max(candidates, key=lambda k: candidates[k])
    return candidates[entity], entity


# --------------------------------------------------------------------------
# Event channel
# --------------------------------------------------------------------------
def _effect_event(n: WindowSnapshot, f: WindowSnapshot) -> list[SubEffect]:
    if n.events is None or f.events is None:
        return [_unusable(k, "event stats missing") for k in SUBSTATS["event"]]

    out: list[SubEffect] = []

    z = rate_ratio_z(
        f.events.warning_count, f.spec.minutes, n.events.warning_count, n.spec.minutes
    )
    if z is None:
        out.append(_unusable("event.warn_rate_z", "no warning events in either window"))
    else:
        out.append(
            SubEffect(
                key="event.warn_rate_z",
                channel="event",
                statistic=z,
                detail={
                    "fault_per_min": f.events.warning_count / f.spec.minutes,
                    "normal_per_min": n.events.warning_count / n.spec.minutes,
                },
            )
        )

    # Container restarts and pod churn are counts, not test statistics.
    # They are differenced so they negate under window swap, which is what
    # calibration needs. A container restart is categorically explicit --
    # a human on-call would see it immediately -- so it earns its own
    # sub-statistic rather than being folded into the event rate.
    out.append(
        SubEffect(
            key="event.restart_delta",
            channel="event",
            statistic=float(f.events.restart_delta - n.events.restart_delta),
            detail={"fault": f.events.restart_delta, "normal": n.events.restart_delta},
        )
    )
    out.append(
        SubEffect(
            key="event.pod_churn",
            channel="event",
            statistic=float(f.events.pod_churn - n.events.pod_churn),
            detail={"fault": f.events.pod_churn, "normal": n.events.pod_churn},
            note="rollout restart shows up here; sham calibration subtracts it",
        )
    )
    return out


# --------------------------------------------------------------------------
# Log channel
# --------------------------------------------------------------------------
def _effect_log(n: WindowSnapshot, f: WindowSnapshot) -> list[SubEffect]:
    key = "log.error_rate_z"
    if n.logs is None or f.logs is None:
        return [_unusable(key, "log stats missing")]

    scored: dict[str, float] = {}
    for pod, (f_err, f_tot) in f.logs.per_pod.items():
        if pod not in n.logs.per_pod:
            continue
        n_err, n_tot = n.logs.per_pod[pod]
        z = two_proportion_z(f_err, f_tot, n_err, n_tot)
        if z is not None:
            scored[pod] = z

    best = _max_over(scored)
    if best is None:
        return [_unusable(key, "no pod had comparable log volume in both windows")]

    value, pod = best
    truncated = f.logs.truncated or n.logs.truncated
    return [
        SubEffect(
            key=key,
            channel="log",
            statistic=value,
            note=f"max over {len(scored)} pods; worst={pod}"
            + ("; TRUNCATED logs bias rates" if truncated else ""),
            detail={"n_entities": len(scored)},
        )
    ]


# --------------------------------------------------------------------------
# Metric channel
# --------------------------------------------------------------------------
def _effect_metric(n: WindowSnapshot, f: WindowSnapshot) -> list[SubEffect]:
    out: list[SubEffect] = []

    # --- trace error rate ---
    key = "metric.trace_error_z"
    if n.traces is None or f.traces is None:
        out.append(_unusable(key, "trace stats missing"))
    else:
        scored: dict[str, float] = {}
        for svc, f_red in f.traces.per_service.items():
            n_red = n.traces.per_service.get(svc)
            if n_red is None:
                continue
            z = two_proportion_z(
                f_red.error_spans, f_red.spans, n_red.error_spans, n_red.spans
            )
            if z is not None:
                scored[svc] = z
        best = _max_over(scored)
        if best is None:
            out.append(_unusable(key, "no service had spans in both windows"))
        else:
            value, svc = best
            out.append(
                SubEffect(
                    key=key,
                    channel="metric",
                    statistic=value,
                    note=f"max over {len(scored)} services; worst={svc}",
                    detail={"n_entities": len(scored)},
                )
            )

    # --- latency ---
    key = "metric.latency_bm_z"
    if n.traces is None or f.traces is None:
        out.append(_unusable(key, "trace stats missing"))
    else:
        scored = {}
        for svc, f_red in f.traces.per_service.items():
            n_red = n.traces.per_service.get(svc)
            if n_red is None:
                continue
            z = brunner_munzel_z_from_hist(f_red.dur_hist_ms, n_red.dur_hist_ms)
            if z is not None:
                scored[svc] = z
        best = _max_over(scored)
        if best is None:
            out.append(_unusable(key, "no service had spans in both windows"))
        else:
            value, svc = best
            out.append(
                SubEffect(
                    key=key,
                    channel="metric",
                    statistic=value,
                    note=f"max over {len(scored)} services; slowest={svc}",
                    detail={"n_entities": len(scored)},
                )
            )

    # --- resources ---
    key = "metric.resource_t"
    if n.resources is None or f.resources is None:
        out.append(_unusable(key, "resource stats missing"))
    else:
        scored = {}
        for name, f_s in f.resources.series.items():
            n_s = n.resources.series.get(name)
            if n_s is None:
                continue
            if f_s.n < MIN_RESOURCE_SAMPLES or n_s.n < MIN_RESOURCE_SAMPLES:
                continue
            t = welch_t(f_s.mean, f_s.var, f_s.n, n_s.mean, n_s.var, n_s.n)
            if t is not None:
                scored[name] = t
        best = _max_over(scored)
        if best is None:
            out.append(
                _unusable(
                    key,
                    f"no series had >={MIN_RESOURCE_SAMPLES} scrapes in both windows "
                    "(expected at a 1m scrape interval on short windows)",
                )
            )
        else:
            value, name = best
            out.append(
                SubEffect(
                    key=key,
                    channel="metric",
                    statistic=value,
                    note=f"max over {len(scored)} series; worst={name}",
                    detail={"n_entities": len(scored)},
                )
            )

    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def compute_effects(
    normal: WindowSnapshot, fault: WindowSnapshot
) -> dict[str, SubEffect]:
    """All sub-statistics for one window pair, keyed by sub-statistic id."""
    if normal.schema_version != fault.schema_version:
        raise ValueError(
            f"schema mismatch: normal={normal.schema_version} fault={fault.schema_version}"
        )
    effects = _effect_event(normal, fault) + _effect_log(normal, fault) + _effect_metric(
        normal, fault
    )
    by_key = {e.key: e for e in effects}
    missing = set(ALL_SUBSTATS) - set(by_key)
    if missing:
        raise AssertionError(f"compute_effects failed to emit {sorted(missing)}")
    return by_key


def verdict(effects: dict[str, SubEffect], thr: Thresholds) -> Verdict:
    """Apply calibrated thresholds. A channel fires if any usable
    sub-statistic in it exceeds its own threshold."""
    per_channel: dict[str, int] = {}
    unusable: list[str] = []
    margins: dict[str, float] = {}
    fired: list[str] = []

    for channel, keys in SUBSTATS.items():
        usable_here = [k for k in keys if k in effects and effects[k].usable]
        if not usable_here:
            unusable.append(channel)
            continue
        hit = False
        for key in usable_here:
            tau = thr.tau.get(key)
            if tau is None:
                continue
            margin = effects[key].statistic - tau
            margins[key] = margin
            if margin > 0.0:
                hit = True
                fired.append(key)
        per_channel[channel] = 1 if hit else 0

    n_usable = len(per_channel)
    total = sum(per_channel.values())
    return Verdict(
        per_channel=per_channel,
        unusable_channels=unusable,
        explicitness=total,
        n_usable_channels=n_usable,
        explicitness_frac=(total / n_usable) if n_usable else None,
        margins=margins,
        fired=fired,
    )


def score_pair(
    normal: WindowSnapshot, fault: WindowSnapshot, thr: Thresholds
) -> tuple[dict[str, SubEffect], Verdict]:
    effects = compute_effects(normal, fault)
    return effects, verdict(effects, thr)


def channels_of(keys: list[str]) -> set[Channel]:
    return {SUBSTAT_CHANNEL[k] for k in keys}
