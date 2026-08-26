"""Metric registration and lookup.

A registry rather than a hardcoded list because metrics arrive over time and in
different categories, and because the evaluation runner should not need editing
to gain one. Registration is by decorator at import time; the package __init__
imports every metric module so a plain ``import`` of the package is enough to
populate it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.qa.metrics.base import BaseMetric, MetricCategory

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# Category evaluation order. Validation first: if the run is not trustworthy,
# nothing computed after it means anything.
CATEGORY_ORDER: tuple[MetricCategory, ...] = (
    MetricCategory.VALIDATION,
    MetricCategory.ACCURACY,
    MetricCategory.EXPERIENCE,
    MetricCategory.DIAGNOSTIC,
)

_REGISTRY: dict[str, BaseMetric] = {}


class DuplicateMetricError(ValueError):
    """Raised when two metrics claim the same name.

    Fatal rather than last-one-wins: a silently shadowed metric would drop out of
    every result set with nothing to notice it by.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"A metric named {name!r} is already registered")


class UnknownMetricError(KeyError):
    """Raised when a metric is requested by a name nothing registered."""

    def __init__(self, name: str) -> None:
        super().__init__(f"No metric named {name!r}. Known: {sorted(_REGISTRY)}")


def register(metric_cls: type[BaseMetric]) -> type[BaseMetric]:
    """Class decorator adding a metric to the registry."""
    instance = metric_cls()
    if instance.name in _REGISTRY:
        raise DuplicateMetricError(instance.name)
    _REGISTRY[instance.name] = instance
    return metric_cls


def get(name: str) -> BaseMetric:
    """Look up one metric by name."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownMetricError(name) from exc


def all_metrics(
    categories: Iterable[MetricCategory] | None = None,
) -> Iterator[BaseMetric]:
    """Every registered metric, in category order then alphabetically.

    Ordering is stable so a results table reads the same way every run, and so
    the validation gates always come first.
    """
    wanted = set(categories) if categories is not None else set(CATEGORY_ORDER)
    for category in CATEGORY_ORDER:
        if category not in wanted:
            continue
        matching = [m for m in _REGISTRY.values() if m.category == category]
        yield from sorted(matching, key=lambda m: m.name)


def registered_names() -> tuple[str, ...]:
    """Names of every registered metric, sorted."""
    return tuple(sorted(_REGISTRY))


def _reset_for_tests() -> None:
    """Clear the registry. Only for tests that register throwaway metrics."""
    _REGISTRY.clear()
