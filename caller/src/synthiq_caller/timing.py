"""Response time, re-asks and call length, from a run's timeline (audit C7).

* Response time: from the end of the caller's answer to the first audio the agent
  sends after it (the director notes ``agent_speech`` when a stretch of agent
  speech starts). The end of the answer is when it was queued plus its length, so
  a call that queued behind another answer reads a little long. A turn the agent
  answered over the caller, or never answered, has no response time.
* Re-asks: answers the caller had to give again, that is answers beyond the
  first to the same step of the scenario.
* Call length: the run's own ``duration_s``.

A figure the run does not carry is None and is printed as "not measured", never 0.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

NOT_MEASURED = "not measured"


@dataclass
class CallTiming:
    latencies_s: list[float] = field(default_factory=list)
    # None: the run does not say what the caller answered.
    reasks: int | None = None
    length_s: float | None = None
    # Whether the run recorded when the agent started speaking at all.
    has_agent_speech: bool = False


def measure(payload: dict[str, Any]) -> CallTiming:
    record = payload.get("record") or {}
    timeline = record.get("timeline") or []
    onsets = sorted(float(e["t"]) for e in timeline if e.get("kind") == "agent_speech" and "t" in e)
    said = record.get("said")
    out = CallTiming(has_agent_speech=bool(onsets))
    if isinstance(said, list):
        seen: set[int] = set()
        reasks = 0
        for answer in said:
            step = answer.get("step_index", -1)
            if step is None or step < 0:
                continue
            reasks += step in seen
            seen.add(step)
        out.reasks = reasks
    duration = record.get("duration_s")
    if isinstance(duration, int | float) and not isinstance(duration, bool) and duration > 0:
        out.length_s = float(duration)
    answers = [
        e for e in timeline if e.get("kind") == "said" and (e.get("seconds") or 0) > 0 and "t" in e
    ]
    for index, answer in enumerate(answers):
        end = float(answer["t"]) + float(answer["seconds"])
        following = float(answers[index + 1]["t"]) if index + 1 < len(answers) else math.inf
        first = next((t for t in onsets if t >= end), None)
        if first is not None and first < following:
            out.latencies_s.append(round(first - end, 3))
    return out


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


@dataclass
class TimingTally:
    latencies_s: list[float] = field(default_factory=list)
    reasks: int = 0
    calls_with_reask_data: int = 0
    lengths_s: list[float] = field(default_factory=list)
    calls: int = 0

    def add(self, one: CallTiming) -> None:
        self.calls += 1
        self.latencies_s += one.latencies_s
        if one.reasks is not None:
            self.reasks += one.reasks
            self.calls_with_reask_data += 1
        if one.length_s is not None:
            self.lengths_s.append(one.length_s)

    def merge(self, other: TimingTally) -> None:
        self.latencies_s += other.latencies_s
        self.reasks += other.reasks
        self.calls_with_reask_data += other.calls_with_reask_data
        self.lengths_s += other.lengths_s
        self.calls += other.calls

    def to_json(self) -> dict[str, Any]:
        lat, length = self.latencies_s, self.lengths_s
        return {
            "response_turns": len(lat),
            "response_median_s": round(statistics.median(lat), 2) if lat else None,
            "response_p90_s": round(percentile(lat, 0.9), 2) if lat else None,
            "reasks": self.reasks if self.calls_with_reask_data else None,
            "reask_calls": self.calls_with_reask_data,
            "length_calls": len(length),
            "length_median_s": round(statistics.median(length), 1) if length else None,
            "length_p90_s": round(percentile(length, 0.9), 1) if length else None,
            "calls": self.calls,
        }


def describe_timing(card: dict[str, Any]) -> list[str]:
    """The three figures as report lines; a figure with no data says so."""
    latency = (
        f"median {card['response_median_s']} s, p90 (nearest-rank) {card['response_p90_s']} s "
        f"over {card['response_turns']} turn(s)"
        if card["response_turns"]
        else NOT_MEASURED
    )
    reasks = (
        f"{card['reasks']} over {card['reask_calls']} call(s)"
        if card["reasks"] is not None
        else NOT_MEASURED
    )
    length = (
        f"median {card['length_median_s']} s, p90 (nearest-rank) {card['length_p90_s']} s "
        f"over {card['length_calls']} call(s)"
        if card["length_calls"]
        else NOT_MEASURED
    )
    return [
        f"response time (agent first audio after the caller's answer): {latency}",
        f"re-asks (caller had to answer the same step again): {reasks}",
        f"call length: {length}",
    ]
