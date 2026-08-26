"""Event-loop lag gauge — the per-process saturation signal for voice workloads.

A voice call is soft-real-time: audio frames arrive every 20 ms and must be
forwarded promptly. When the event loop is saturated it wakes *late* from
``asyncio.sleep`` — a task that asked to sleep 100 ms resumes at 100 + X ms.
That overshoot X is the cleanest provider-independent measure of how close a
process is to its real ceiling.

It beats the obvious alternatives:

* **CPU%** is measured against the container limit and reads far below 100% at
  true saturation, because the loop is blocked rather than busy.
* **Turn latency** is dominated by external STT/LLM/TTS round-trips, so it moves
  for reasons that have nothing to do with this process.

Read p95 off ``GET /health/loop-lag`` to size calls-per-process: ramp concurrent
calls against one process and find the knee where p95 lag starts climbing.

Approach adapted from Dograh (BSD 2-Clause, https://github.com/dograh-hq/dograh).
"""

from __future__ import annotations

import asyncio
import contextlib

# One gauge per process — exactly the unit (a single event loop) being sized.
# A bounded window of recent samples is enough for percentiles; no metrics
# library, no lock (single loop, single writer).
_INTERVAL = 0.1  # seconds between probes
_WINDOW = 600  # ~60s of history at the probe interval

_samples: list[float] = []
# asyncio holds only a weak reference to tasks, so without a strong ref here the
# monitor can be garbage-collected mid-run and the gauge silently dies.
_task: asyncio.Task[None] | None = None


async def _monitor() -> None:
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(_INTERVAL)
        lag_ms = (loop.time() - t0 - _INTERVAL) * 1000
        _samples.append(max(lag_ms, 0.0))  # clock jitter can go slightly negative
        if len(_samples) > _WINDOW:
            del _samples[: len(_samples) - _WINDOW]


def start() -> None:
    """Begin sampling. Idempotent — safe to call from a lifespan hook."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_monitor(), name="loop-lag-monitor")


async def stop() -> None:
    """Stop sampling and discard history."""
    global _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    _samples.clear()


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list."""
    if not values:
        return 0.0
    rank = max(1, min(len(values), round(pct / 100 * len(values))))
    return values[rank - 1]


def snapshot() -> dict[str, float | int | bool]:
    """Current lag statistics. Never raises; safe to call before ``start()``."""
    ordered = sorted(_samples)
    return {
        "running": _task is not None and not _task.done(),
        "samples": len(ordered),
        "window_seconds": round(_WINDOW * _INTERVAL, 1),
        "p50_ms": round(_percentile(ordered, 50), 2),
        "p95_ms": round(_percentile(ordered, 95), 2),
        "p99_ms": round(_percentile(ordered, 99), 2),
        "max_ms": round(ordered[-1], 2) if ordered else 0.0,
    }
