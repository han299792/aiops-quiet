"""Calibrate per-sub-statistic thresholds from the sham null distribution.

PURE MODULE.

The null pairs come from the SHAM arm: two consecutive steady-state
windows from a run where ``paymentFailure`` was set to ``off`` through
exactly the same configmap write and ``flagd`` rollout restart as every
other dose. Both windows are genuinely normal, so any statistic they
produce is noise -- including whatever the injection machinery itself
stirs up. Calibrating here is what subtracts the injection artifact.

Using the noop problem instead would be wrong: ``NoopFaultInjector`` does
nothing at all, so its null would contain no rollout churn, and every
real dose would then "fire" the event channel purely because flagd
restarted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .model import ALL_SUBSTATS, SUBSTATS, Thresholds, WindowSnapshot
from .score import compute_effects
from .stats import mad, median, quantile, z_quantile

NullPair = tuple[WindowSnapshot, WindowSnapshot]


def min_n_for_quantile(m: int, alpha: float) -> int:
    """Null observations needed before the empirical quantile is an estimate.

    The corrected target is the ``1 - alpha/m`` quantile. Estimating it
    empirically needs enough observations that it is not simply the
    largest value seen; the rule of thumb is a couple of observations
    beyond the tail, i.e. ``n >= 2m/alpha``.

    In practice this bites hard. With alpha=0.05 and a 9-way correction
    the requirement is 360 null observations = 180 sham runs, which at
    ten-plus minutes a run is not affordable. So the MAD path below is
    the one that will actually be taken, and it is built to hit the same
    corrected quantile rather than a round number of sigmas.
    """
    import math

    return math.ceil(2.0 * m / alpha)


def expand_orderings(pairs: list[NullPair]) -> list[NullPair]:
    """Emit both orderings of each null pair.

    Under the null the two windows are exchangeable, so (a, b) and (b, a)
    are both valid draws from the null distribution and using both
    doubles the sample.

    Two caveats worth stating plainly, because they were got wrong once
    already:

    1. The two draws from one pair are DEPENDENT, so the effective sample
       size is below 2n and the quantile estimate is noisier than 2n
       independent draws would suggest.
    2. This does NOT make the null symmetric about zero for the
       max-over-entities statistics. Swapping turns ``max_i z_i`` into
       ``max_i(-z_i) == -min_i z_i``, not ``-max_i z_i``. Their null is
       legitimately right-shifted, and the threshold is calibrated on
       that shifted null, which is exactly the point. Only the plain
       difference statistics (restart delta, pod churn) symmetrize.
    """
    return [p for a, b in pairs for p in ((a, b), (b, a))]


def null_statistics(pairs: list[NullPair]) -> dict[str, list[float]]:
    """Collect the null distribution of every sub-statistic.

    Unusable observations are dropped rather than recorded as 0, so the
    null of a sub-statistic reflects only the runs where it was actually
    measurable.
    """
    out: dict[str, list[float]] = {k: [] for k in ALL_SUBSTATS}
    for normal, fault in expand_orderings(pairs):
        effects = compute_effects(normal, fault)
        for key, eff in effects.items():
            if eff.usable:
                out[key].append(eff.statistic)
    return out


def _correction_denominator() -> dict[str, int]:
    """Family-wise correction, split across channels then within a channel.

    Bonferroni over the k=3 channels, and within a channel over its own
    sub-statistics, so the total family-wise false-positive rate across
    the whole verdict is bounded by alpha. Without this, three channels
    each at 95% give roughly a 14% chance that a genuinely null run shows
    non-zero explicitness -- which would show up as the sham arm looking
    "a bit explicit" and would poison the bottom of the dose curve.
    """
    k_channels = len(SUBSTATS)
    return {
        key: k_channels * len(keys)
        for keys in SUBSTATS.values()
        for key in keys
    }


def calibrate(
    pairs: list[NullPair],
    *,
    alpha: float = 0.05,
    fitted_at: datetime | None = None,
) -> Thresholds:
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    nulls = null_statistics(pairs)
    corrections = _correction_denominator()

    tau: dict[str, float | None] = {}
    method: dict[str, str] = {}
    n_null: dict[str, int] = {}
    summary: dict[str, dict[str, float]] = {}

    for key in ALL_SUBSTATS:
        sample = nulls[key]
        n_null[key] = len(sample)
        m = corrections[key]
        if not sample:
            # Never measurable in the null: no basis for a threshold, so
            # the statistic is disabled. None (not +inf) because this has
            # to survive a JSON round-trip -- see Thresholds.tau.
            tau[key] = None
            method[key] = "mad_fallback"
            summary[key] = {}
            continue

        s = sorted(sample)
        med = median(s)
        spread = mad(s)
        summary[key] = {
            "min": s[0],
            "median": med,
            "mad": spread,
            "max": s[-1],
            "n": float(len(s)),
        }

        target_q = 1.0 - alpha / m
        if len(s) >= min_n_for_quantile(m, alpha):
            tau[key] = float(quantile(s, target_q))
            method[key] = "empirical_quantile"
        else:
            # Robust parametric stand-in aimed at the SAME corrected
            # quantile: median + z_{1-alpha/m} * MAD. The z-statistics are
            # approximately normal under the null by construction, and
            # median/MAD are the robust location and scale, so this is the
            # corrected threshold with the tail estimated parametrically
            # instead of counted -- not an arbitrary number of sigmas.
            tau[key] = med + z_quantile(target_q) * spread
            method[key] = "mad_fallback"

        # A degenerate null (every observation identical -- restart counts
        # that are always 0, say) has mad == 0, so the threshold collapses
        # onto the constant. Nudge above it: an event that never once
        # happened in the null should fire the first time it happens, but
        # a value merely EQUAL to the constant should not.
        current = tau[key]
        if spread == 0.0 and current is not None and current <= med:
            tau[key] = med + 1e-9

    return Thresholds(
        tau=tau,
        method=method,  # type: ignore[arg-type]
        alpha=alpha,
        correction=corrections,
        n_null=n_null,
        fitted_at=fitted_at or datetime.now(timezone.utc),
        null_summary=summary,
    )


def evaluate_null(thr: Thresholds, held_out: list[NullPair]) -> dict[str, float]:
    """Realized false-positive rate on held-out null pairs.

    Fit on one half, measure here on the other. If the realized rate is
    far above ``alpha`` the two windows are not exchangeable -- usually
    the warmup was too short, or the load generator was still ramping,
    so the "normal" window is not actually steady state.

    Returns per-sub-statistic rates plus ``__any_channel__``, the rate at
    which a genuinely null pair scores non-zero explicitness. That last
    number is the one to report: it is the sham arm's measured
    explicitness error.
    """
    from .score import verdict  # local import keeps the module graph acyclic

    expanded = expand_orderings(held_out)
    if not expanded:
        return {}

    fires: dict[str, int] = {k: 0 for k in ALL_SUBSTATS}
    usable: dict[str, int] = {k: 0 for k in ALL_SUBSTATS}
    any_channel = 0

    for normal, fault in expanded:
        effects = compute_effects(normal, fault)
        for key, eff in effects.items():
            if not eff.usable:
                continue
            tau = thr.tau.get(key)
            if tau is None:
                continue  # statistic disabled; not part of the family
            usable[key] += 1
            if eff.statistic > tau:
                fires[key] += 1
        if verdict(effects, thr).explicitness > 0:
            any_channel += 1

    rates = {k: (fires[k] / usable[k]) for k in ALL_SUBSTATS if usable[k]}
    rates["__any_channel__"] = any_channel / len(expanded)
    return rates


def split_half(pairs: list[NullPair], *, seed: int = 0) -> tuple[list[NullPair], list[NullPair]]:
    """Deterministic random half-split for fit/held-out."""
    import random

    rng = random.Random(seed)
    shuffled = list(pairs)
    rng.shuffle(shuffled)
    mid = len(shuffled) // 2
    return shuffled[:mid], shuffled[mid:]
