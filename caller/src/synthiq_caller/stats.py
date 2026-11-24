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


def describe(passes: int, total: int) -> str:
    if total <= 0:
        return "no runs"
    low, high = wilson_interval(passes, total)
    return f"{passes} of {total} ({passes / total:.0%}; 95% interval {low:.0%} to {high:.0%})"
