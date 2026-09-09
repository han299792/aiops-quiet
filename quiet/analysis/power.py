"""How much telemetry does the bottom of the dose curve actually need?

PURE MODULE.

The point of running this before any cluster time: if a 10-minute window
cannot separate a 10% dose from sham, then the low end of the dose-response
curve is noise, and no amount of agent spend will fix that. The fix would
be a longer window, not a different statistic -- and it is much cheaper to
learn that here than after a two-day campaign.

The model is deliberately simple and its assumptions are parameters, not
constants, because the real values are unknown until the first cluster
session measures them:

* ``spans_per_service`` -- span volume per service per window. Depends on
  the load generator's rate and on Jaeger's sampling, which the OTel demo
  may set aggressively. This is the number stage B has to measure.
* ``baseline_error_rate`` -- how error-y the app is when healthy. Never
  exactly zero.
* ``charge_fraction`` -- the share of the payment service's spans that are
  charge requests, since ``paymentFailure`` only fails those. A dose of
  10% therefore does NOT mean a 10% service error rate.

Detection here means "the metric channel fires against a calibrated
threshold", which is the same rule the real pipeline applies.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..probe.stats import two_proportion_z, z_quantile

#: Family-wise share for a metric-channel sub-statistic: alpha over three
#: channels over three sub-statistics in the metric channel. Matches
#: quiet.probe.calibrate._correction_denominator.
METRIC_CORRECTION = 9


@dataclass(frozen=True)
class Scenario:
    spans_per_service: int
    baseline_error_rate: float = 0.002
    charge_fraction: float = 0.25
    n_services: int = 12
    alpha: float = 0.05

    def fault_error_rate(self, dose: float) -> float:
        """Service-level error rate under a given dose.

        ``paymentFailure`` fails a fraction ``dose`` of charge requests,
        and charges are only ``charge_fraction`` of the service's spans,
        so the observable rate moves by ``dose * charge_fraction`` -- a
        10% dose is roughly a 2.5% service error rate at the default.
        """
        p = self.baseline_error_rate + dose * self.charge_fraction
        return min(p, 1.0)


def analytic_threshold(scenario: Scenario, *, n_entities: int | None = None) -> float:
    """Approximate the calibrated threshold for the max over services.

    The real threshold comes from the sham null. This mirrors it closely
    enough for planning: the null of a maximum over ``k`` roughly
    independent standard normals sits near its ``1 - (alpha/m)/k``
    quantile.
    """
    k = n_entities if n_entities is not None else scenario.n_services
    per_test = (scenario.alpha / METRIC_CORRECTION) / max(k, 1)
    return z_quantile(1.0 - per_test)


def _draw_z(rng: random.Random, scenario: Scenario, dose: float) -> float | None:
    """One simulated max-over-services trace-error z."""
    n = scenario.spans_per_service
    p0 = scenario.baseline_error_rate
    p1 = scenario.fault_error_rate(dose)

    best: float | None = None
    for idx in range(scenario.n_services):
        rate_fault = p1 if idx == 0 else p0  # only the payment service is hit
        e_f = sum(1 for _ in range(n) if rng.random() < rate_fault)
        e_n = sum(1 for _ in range(n) if rng.random() < p0)
        z = two_proportion_z(e_f, n, e_n, n)
        if z is not None and (best is None or z > best):
            best = z
    return best


def detection_rate(
    scenario: Scenario, dose: float, *, trials: int = 400, seed: int = 0
) -> float:
    """Fraction of trials where the metric channel fires at this dose.

    At ``dose == 0`` this is the false-positive rate and should land near
    ``alpha / METRIC_CORRECTION``.
    """
    rng = random.Random(seed)
    tau = analytic_threshold(scenario)
    hits = 0
    for _ in range(trials):
        z = _draw_z(rng, scenario, dose)
        if z is not None and z > tau:
            hits += 1
    return hits / trials


def min_detectable_dose(
    scenario: Scenario,
    doses: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 1.00),
    *,
    target_power: float = 0.80,
    trials: int = 400,
    seed: int = 0,
) -> float | None:
    """Lowest dose in ``doses`` reaching ``target_power``, or None."""
    for dose in doses:
        if detection_rate(scenario, dose, trials=trials, seed=seed) >= target_power:
            return dose
    return None


def spans_needed_for(
    dose: float,
    *,
    template: Scenario,
    candidates: tuple[int, ...] = (200, 500, 1000, 2000, 5000, 10000, 20000, 50000),
    target_power: float = 0.80,
    trials: int = 400,
    seed: int = 0,
) -> int | None:
    """Smallest per-service span count that detects ``dose`` reliably.

    Divide the answer by the observed spans-per-minute to get the window
    length the campaign actually needs.
    """
    from dataclasses import replace

    for n in candidates:
        scenario = replace(template, spans_per_service=n)
        if detection_rate(scenario, dose, trials=trials, seed=seed) >= target_power:
            return n
    return None


def sweep(
    template: Scenario,
    span_counts: tuple[int, ...] = (500, 1000, 2000, 5000, 10000, 20000),
    doses: tuple[float, ...] = (0.0, 0.10, 0.25, 0.50, 1.00),
    *,
    trials: int = 300,
    seed: int = 0,
) -> dict[int, dict[float, float]]:
    """Detection rate over the (span volume x dose) grid."""
    from dataclasses import replace

    out: dict[int, dict[float, float]] = {}
    for i, n in enumerate(span_counts):
        scenario = replace(template, spans_per_service=n)
        out[n] = {
            d: detection_rate(scenario, d, trials=trials, seed=seed + i * 1000 + j)
            for j, d in enumerate(doses)
        }
    return out


def format_sweep(table: dict[int, dict[float, float]], template: Scenario) -> str:
    doses = sorted(next(iter(table.values())).keys()) if table else []
    header = "spans/svc | " + " | ".join(f"{d:>5.0%}" for d in doses)
    lines = [
        f"assumptions: baseline_error={template.baseline_error_rate:.3%}, "
        f"charge_fraction={template.charge_fraction:.0%}, "
        f"services={template.n_services}, alpha={template.alpha}",
        "detection rate of the metric channel (dose 0% column = false positive rate)",
        "",
        header,
        "-" * len(header),
    ]
    for n, row in sorted(table.items()):
        lines.append(f"{n:>9} | " + " | ".join(f"{row[d]:>5.0%}" for d in doses))
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - CLI convenience
    template = Scenario(spans_per_service=2000)
    table = sweep(template)
    print(format_sweep(table, template))
    print()
    for dose in (0.10, 0.25):
        need = spans_needed_for(dose, template=template)
        if need is None:
            print(f"dose {dose:.0%}: not detectable at 80% power within the grid")
        else:
            print(f"dose {dose:.0%}: needs >= {need} spans/service/window at 80% power")


if __name__ == "__main__":  # pragma: no cover
    main()
