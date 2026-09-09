"""Builders for synthetic snapshots.

Used by the unit tests and by the power simulation. Everything here is
deterministic given a seed, so a failing test reproduces exactly.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from quiet.probe.model import (
    EventStats,
    LogStats,
    ResourceStats,
    ServiceRED,
    SeriesStats,
    TraceStats,
    WindowSnapshot,
    WindowSpec,
    make_hist,
)

T0 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def spec(
    label: str = "normal",
    *,
    minutes: float = 10.0,
    variant: str = "off",
    run_id: str = "r0",
    problem_id: str = "payment_dose_00-detection-1",
    offset_min: float = 0.0,
) -> WindowSpec:
    start = T0 + timedelta(minutes=offset_min)
    return WindowSpec(
        run_id=run_id,
        problem_id=problem_id,
        variant=variant,
        label=label,  # type: ignore[arg-type]
        namespace="astronomy-shop",
        t_start=start,
        t_end=start + timedelta(minutes=minutes),
    )


def lognormal_ms(rng: random.Random, n: int, *, median_ms: float, sigma: float) -> list[float]:
    import math

    mu = math.log(median_ms)
    return [math.exp(rng.gauss(mu, sigma)) for _ in range(n)]


def service_red(
    rng: random.Random,
    *,
    spans: int,
    error_rate: float,
    median_ms: float,
    sigma: float = 0.6,
) -> ServiceRED:
    errors = sum(1 for _ in range(spans) if rng.random() < error_rate)
    durations = lognormal_ms(rng, spans, median_ms=median_ms, sigma=sigma)
    return ServiceRED(spans=spans, error_spans=errors, dur_hist_ms=make_hist(durations))


def snapshot(
    rng: random.Random,
    *,
    label: str = "normal",
    minutes: float = 10.0,
    variant: str = "off",
    services: dict[str, tuple[int, float, float]] | None = None,
    pods: dict[str, tuple[int, float]] | None = None,
    warning_events: int = 0,
    restart_delta: int = 0,
    pod_churn: int = 0,
    resource_series: dict[str, tuple[float, float, int]] | None = None,
    **spec_kw,
) -> WindowSnapshot:
    """Build a snapshot.

    ``services``: name -> (spans, error_rate, median_latency_ms)
    ``pods``: name -> (total_lines, error_rate)
    ``resource_series``: name -> (mean, var, n)
    """
    services = services or {"payment": (2000, 0.001, 25.0), "cart": (2000, 0.001, 15.0)}
    pods = pods or {"payment-0": (5000, 0.001), "cart-0": (5000, 0.001)}
    resource_series = resource_series or {
        "payment-0|cpu": (0.10, 0.0004, 10),
        "cart-0|cpu": (0.08, 0.0004, 10),
    }

    per_service = {
        name: service_red(rng, spans=n, error_rate=er, median_ms=ms)
        for name, (n, er, ms) in services.items()
    }
    total = ServiceRED(
        spans=sum(s.spans for s in per_service.values()),
        error_spans=sum(s.error_spans for s in per_service.values()),
    )

    per_pod: dict[str, tuple[int, int]] = {}
    for name, (total_lines, er) in pods.items():
        errs = sum(1 for _ in range(total_lines) if rng.random() < er)
        per_pod[name] = (errs, total_lines)

    return WindowSnapshot(
        spec=spec(label, minutes=minutes, variant=variant, **spec_kw),
        events=EventStats(
            warning_count=warning_events,
            normal_count=0,
            restart_delta=restart_delta,
            pod_churn=pod_churn,
        ),
        logs=LogStats(
            total_lines=sum(t for _, t in per_pod.values()),
            error_lines=sum(e for e, _ in per_pod.values()),
            per_pod=per_pod,
        ),
        traces=TraceStats(per_service=per_service, total=total),
        resources=ResourceStats(
            series={
                name: SeriesStats(mean=m, var=v, n=n)
                for name, (m, v, n) in resource_series.items()
            }
        ),
    )


def null_pair(rng: random.Random, **kw) -> tuple[WindowSnapshot, WindowSnapshot]:
    """Two windows drawn from the SAME distribution -- a genuine null."""
    a = snapshot(rng, label="normal", offset_min=0.0, **kw)
    b = snapshot(rng, label="fault", offset_min=10.0, **kw)
    return a, b


def dose_pair(
    rng: random.Random, *, payment_error_rate: float, **kw
) -> tuple[WindowSnapshot, WindowSnapshot]:
    """A normal window and a fault window where `payment` fails at a rate."""
    normal = snapshot(rng, label="normal", offset_min=0.0, **kw)
    fault = snapshot(
        rng,
        label="fault",
        offset_min=10.0,
        services={
            "payment": (2000, max(payment_error_rate, 0.001), 25.0),
            "cart": (2000, 0.001, 15.0),
        },
        **kw,
    )
    return normal, fault
