"""Test statistics for the explicitness channels.

PURE MODULE -- stdlib only, no scipy. Everything here is a plain function
over numbers so it can be unit-tested against known values.

Every statistic is SIGNED and oriented so that larger means "the fault
window looks more anomalous than the normal window". Thresholds are
calibrated empirically from the sham null, so a statistic does not have
to be exactly N(0,1) -- but the ones that are give a free sanity check:
their calibrated thresholds should land near 2-3.
"""

from __future__ import annotations

import math
from statistics import NormalDist

_ND = NormalDist()


def two_proportion_z(e_f: int, n_f: int, e_n: int, n_n: int) -> float | None:
    """Two-proportion z-test, fault vs normal.

    ``e_*`` are event counts (error lines, error spans), ``n_*`` the
    corresponding totals. Returns ``None`` when it is undefined: no
    observations at all, or a pooled rate of exactly 0 or 1 (in which
    case there is no variance and no evidence either).

    Positive means the fault window has the higher rate.
    """
    if n_f <= 0 or n_n <= 0:
        return None
    p_f = e_f / n_f
    p_n = e_n / n_n
    pooled = (e_f + e_n) / (n_f + n_n)
    if pooled <= 0.0 or pooled >= 1.0:
        return None
    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_f + 1.0 / n_n))
    if se <= 0.0:
        return None
    return (p_f - p_n) / se


def rate_ratio_z(k_f: int, t_f: float, k_n: int, t_n: float) -> float | None:
    """Poisson rate comparison for counts over unequal exposure times.

    ``k_*`` are counts, ``t_*`` the window lengths in the same unit
    (minutes). Uses the pooled-rate variance, which is the score-test
    form and behaves sanely at small counts.

    Returns ``None`` when either window has no exposure, or when both
    windows saw zero events (no rate, no evidence).
    """
    if t_f <= 0.0 or t_n <= 0.0:
        return None
    total = k_f + k_n
    if total == 0:
        return None
    pooled_rate = total / (t_f + t_n)
    var = pooled_rate * (1.0 / t_f + 1.0 / t_n)
    if var <= 0.0:
        return None
    return (k_f / t_f - k_n / t_n) / math.sqrt(var)


def welch_t(mean_f: float, var_f: float, n_f: int, mean_n: float, var_n: float, n_n: int) -> float | None:
    """Welch's t for two independent samples with unequal variance.

    Returned as a raw t, not converted to a p-value: with n around 10 the
    normal approximation is poor, and since thresholds are calibrated
    empirically from the sham null the exact reference distribution does
    not matter -- only that the statistic is scale-free and monotone.
    """
    if n_f < 2 or n_n < 2:
        return None
    se2 = var_f / n_f + var_n / n_n
    if se2 <= 0.0:
        return None
    return (mean_f - mean_n) / math.sqrt(se2)


def brunner_munzel_z_from_hist(hist_f: list[int], hist_n: list[int]) -> float | None:
    """Normalized Mann-Whitney (stochastic superiority) from two histograms.

    Computes ``A = P(X_f > X_n) + 0.5 * P(X_f == X_n)`` treating values
    inside a bin as tied, then standardizes it. Nonparametric, so a
    long latency tail does not dominate the way a mean shift would, and
    it needs only the binned counts -- no retained spans.

    Positive means the fault window is stochastically slower.
    """
    if len(hist_f) != len(hist_n):
        raise ValueError("histograms must share the same binning")
    n_f = sum(hist_f)
    n_n = sum(hist_n)
    if n_f == 0 or n_n == 0:
        return None

    # A = (# pairs f>n + 0.5 * ties) / (n_f * n_n), computed bin-wise.
    greater = 0.0
    ties = 0.0
    cum_n = 0  # count of normal-window values in strictly lower bins
    for i, f_count in enumerate(hist_f):
        if f_count:
            greater += f_count * cum_n
            ties += f_count * hist_n[i]
        cum_n += hist_n[i]

    a = (greater + 0.5 * ties) / (n_f * n_n)

    # Standard error of A under the null, using the conservative
    # Mann-Whitney variance. Exact BM variance needs per-observation
    # placements; with binned data this bound is the honest choice and
    # errs toward fewer false positives.
    se = math.sqrt((n_f + n_n + 1.0) / (12.0 * n_f * n_n))
    if se <= 0.0:
        return None
    return (a - 0.5) / se


def z_to_p_one_sided(z: float) -> float:
    return 1.0 - _ND.cdf(z)


def z_quantile(p: float) -> float:
    """Standard normal quantile. ``z_quantile(0.95) == 1.645``."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    return _ND.inv_cdf(p)


def quantile(sorted_values: list[float], q: float) -> float:
    """Type-7 empirical quantile (numpy's default) of a SORTED list."""
    if not sorted_values:
        raise ValueError("empty sample")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[int(pos)]
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def median(values: list[float]) -> float:
    s = sorted(values)
    return quantile(s, 0.5)


def mad(values: list[float], *, scale: float = 1.4826) -> float:
    """Median absolute deviation, scaled to be a consistent sigma estimate."""
    if not values:
        raise ValueError("empty sample")
    med = median(values)
    return scale * median([abs(v - med) for v in values])


def wilson_interval(successes: int, n: int, *, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Used instead of the normal approximation because at N=10 the latter
    produces intervals that leave [0, 1].
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError(f"successes={successes} out of range for n={n}")
    z = _ND.inv_cdf(1.0 - (1.0 - confidence) / 2.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)
