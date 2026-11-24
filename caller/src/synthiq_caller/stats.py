"""Pass rates with honest error bars."""

from __future__ import annotations

import math


def wilson_interval(passes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for a pass rate, as (low, high) in 0..1.

    Unlike the plain "rate plus or minus" interval it behaves at the edges:
    5 of 5 does not come out as 100% to 100%.
    """
    if total <= 0:
        return (0.0, 1.0)
    if not 0 <= passes <= total:
        raise ValueError("passes must be between 0 and total")
    rate = passes / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _at_most(failures: int, total: int, rate: float) -> float:
    """The chance of ``failures`` or fewer in ``total`` calls at a true pass rate ``rate``."""
    return sum(
        math.comb(total, k) * (1 - rate) ** k * rate ** (total - k) for k in range(failures + 1)
    )


def calls_needed(rate: float, confidence: float = 0.95, failures: int = 0) -> int:
    """How many independent calls show a true pass rate of at least ``rate``.

    The smallest run in which seeing no more than ``failures`` failures would
    happen less than ``1 - confidence`` of the time if the true rate were only
    ``rate``. For 95% at 95% confidence: 59 calls with none failing, 93 with
    one, 124 with two.
    """
    if not 0 < rate < 1 or not 0 < confidence < 1:
        raise ValueError("rate and confidence must be between 0 and 1")
    total = failures + 1
    while _at_most(failures, total, rate) > 1 - confidence:
        total += 1
    return total


def lower_bound(passes: int, total: int, confidence: float = 0.95) -> float:
    """The exact one-sided lower bound on the true pass rate (Clopper-Pearson)."""
    if total <= 0 or passes <= 0:
        return 0.0
    if passes > total:
        raise ValueError("passes must be between 0 and total")
    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2
        # The chance of this few failures or fewer, were the true rate ``mid``.
        if _at_most(total - passes, total, mid) > 1 - confidence:
            high = mid
        else:
            low = mid
    return low


def gate(
    passes: int, total: int, rate: float = 0.95, confidence: float = 0.95
) -> tuple[bool, float, str]:
    """Whether ``passes`` of ``total`` independent calls demonstrate ``rate``.

    Returns (met, the lower bound shown, a sentence). ``total`` must count every
    call that was answered or should have been: failures of unknown cause
    included, calls the rig spoiled re-run rather than dropped.
    """
    shown = lower_bound(passes, total, confidence)
    failures = total - passes
    need = calls_needed(rate, confidence, failures)
    met = total >= need
    plan = (
        f"{rate:.0%} at {confidence:.0%} confidence needs "
        f"{calls_needed(rate, confidence, 0)} calls with none failing, "
        f"{calls_needed(rate, confidence, 1)} with one, "
        f"{calls_needed(rate, confidence, 2)} with two"
    )
    if total <= 0:
        return False, 0.0, f"no calls to judge. {plan}."
    verdict = (
        f"{passes} of {total} shows a true rate of at least {shown:.1%} "
        f"({confidence:.0%} confidence, one-sided): "
        + ("the gate is met" if met else f"NOT met; with {failures} failure(s) it takes {need}")
    )
    return met, shown, f"{verdict}. {plan}."


def describe(passes: int, total: int) -> str:
    if total <= 0:
        return "no runs"
    low, high = wilson_interval(passes, total)
    return f"{passes} of {total} ({passes / total:.0%}; 95% interval {low:.0%} to {high:.0%})"
