"""One page that says how a set of test calls went, and how that compares with last time.

    synthiq-caller scorecard runs/monday runs/tuesday --out card.html \\
        --previous last-week.html --faults faults.json

The numbers are ``report.summarise`` over each directory, added together; the
comparison is ``compare.compare`` against a baseline. This module adds what a
page needs beyond the text report: failures grouped by fault class and last
step, and each said/heard/stored disagreement with where "stored" came from.

The page is a single file with no outside assets, readable in light and dark.
Its numbers, and a baseline of them, are embedded in it (and written beside it
as ``.json``), so the next scorecard can be compared with it by passing the page
itself as ``--previous``. A file made by ``synthiq-caller baseline`` works too.

**Fault classes.** A failed call says where it stopped, not whose fault it was.
That is a person's judgement, so it comes from a ``--faults`` file mapping
``"<directory name>/<run file>"`` to one of ``FAULT_CLASSES``; a failure nobody
has classified is shown as ``unclassified``, never guessed. The one automatic
class is ``rig``, for a call whose trace could not be read.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synthiq_caller.compare import Baseline, compare, make_baseline
from synthiq_caller.jsonio import write_json
from synthiq_caller.report import FieldTally, Summary, failing_step, summarise
from synthiq_caller.stats import describe, wilson_interval

# "unknown": the call was not answered, died or ran out of time; nobody can say whose doing.
FAULT_CLASSES = ("agent", "scenario", "fixture", "rig", "unknown", "unclassified")
NO_STEP = "(no step recorded)"
_DATA = re.compile(r'<script type="application/json" id="scorecard-data">(.*?)</script>', re.DOTALL)


def merged(summaries: list[Summary]) -> Summary:
    """Several directories' summaries as one."""
    total = Summary()
    for one in summaries:
        total.runs += one.runs
        total.passed += one.passed
        total.failed += one.failed
        total.ungraded += one.ungraded
        total.void += one.void
        total.ungradeable += one.ungradeable
        total.synthetic_calls += one.synthetic_calls
        total.excluded += one.excluded
        for who, count in one.faults.items():
            total.faults[who] = total.faults.get(who, 0) + count
        for name, (passed, graded) in one.by_scenario.items():
            tally = total.by_scenario.setdefault(name, [0, 0])
            tally[0] += passed
            tally[1] += graded
        for name, field_tally in one.fields.items():
            into = total.fields.setdefault(name, FieldTally())
            for key, value in asdict(field_tally).items():
                setattr(into, key, getattr(into, key) + value)
        for name, counts in one.chains.items():
            chain = total.chains.setdefault(name, [0, 0, 0])
            for index, value in enumerate(counts):
                chain[index] += value
        total.failures += one.failures
        total.chain_failures += one.chain_failures
        total.blocked += one.blocked
    return total


def _last_step(payload: dict[str, Any]) -> str:
    path = (payload.get("trace") or {}).get("step_path") or []
    return str(path[-1]) if path else NO_STEP


def _fault(key: str, payload: dict[str, Any], faults: dict[str, str]) -> str:
    # A person's ruling first, then what the grader charged the call to.
    chosen = faults.get(key) or payload.get("fault") or payload["grade"].get("fault") or ""
    if chosen:
        return str(chosen)
    if payload["grade"]["outcome"] not in ("passed", "failed"):
        if payload.get("trace_error") or not payload.get("trace"):
            return "rig"
        return "scenario"  # a trace, and nothing in the scenario that could be checked
    return "unclassified"


def _why_ungraded(payload: dict[str, Any]) -> str:
    if payload["grade"].get("reason"):
        return f"{payload['grade']['outcome']}: {payload['grade']['reason']}"
    if payload.get("trace_error"):
        return str(payload["trace_error"])
    if not payload.get("trace"):
        return "no trace"
    unchecked = [c["what"] for c in payload["grade"]["checks"] if "NOT CHECKED" in c["what"]]
    return "; ".join(unchecked) if unchecked else "no expectations"


def build(
    directories: list[Path] | list[str],
    *,
    faults: dict[str, str] | None = None,
    previous: Baseline | None = None,
    title: str = "",
) -> dict[str, Any]:
    faults = dict(faults or {})
    for key, value in faults.items():
        if value not in FAULT_CLASSES:
            raise ValueError(f"fault class for {key} must be one of {FAULT_CLASSES}, not {value!r}")
    roots = [Path(d) for d in directories]
    summary = merged([summarise(root) for root in roots])

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    not_graded: list[dict[str, Any]] = []
    disagreements: dict[str, list[dict[str, Any]]] = {}
    scenario_ungraded: dict[str, int] = {}
    for root in roots:
        for path in sorted(root.glob("run-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            grade = payload["grade"]
            name, key = grade["scenario"], f"{root.name}/{path.name}"
            if grade["outcome"] not in ("passed", "failed"):
                # void, ungradeable and ungraded: placed, and in neither count.
                scenario_ungraded[name] = scenario_ungraded.get(name, 0) + 1
                not_graded.append(
                    {
                        "run": key,
                        "scenario": name,
                        "fault": _fault(key, payload, faults),
                        "reason": _why_ungraded(payload),
                    }
                )
            elif grade["outcome"] == "failed":
                fault, step = _fault(key, payload, faults), _last_step(payload)
                group = groups.setdefault(
                    (fault, step),
                    {
                        "fault": fault,
                        "last_step": step,
                        "count": 0,
                        "scenarios": [],
                        "runs": [],
                        "checks": [],
                        "stopped_at": failing_step(payload),
                    },
                )
                group["count"] += 1
                group["runs"].append(key)
                if name not in group["scenarios"]:
                    group["scenarios"].append(name)
                for check in grade["checks"]:
                    if not check["passed"] and check["what"] not in group["checks"]:
                        group["checks"].append(check["what"])
            for row in grade["fields"]:
                wrong = row["stored"] is not None and not row["stored_matches"]
                if row["heard_matches"] is False or wrong:
                    disagreements.setdefault(row["field"], []).append(
                        {
                            "run": key,
                            "scenario": name,
                            "said": row["said"],
                            "heard": row["heard"],
                            "stored": row["stored"],
                            "stored_source": row.get("stored_source", ""),
                        }
                    )

    graded = summary.passed + summary.failed
    not_run: dict[tuple[str, str], int] = {}
    for item in summary.blocked:
        pair = (str(item.get("scenario_file", "")), str(item.get("reason", "")))
        not_run[pair] = not_run.get(pair, 0) + 1
    baseline = make_baseline(summary, source=", ".join(root.name for root in roots))
    card: dict[str, Any] = {
        "title": title or "Scorecard",
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "sources": [root.name for root in roots],
        "calls": summary.runs,
        "passed": summary.passed,
        "failed": summary.failed,
        "ungraded": summary.ungraded + summary.void + summary.ungradeable,
        "void": summary.void,
        "ungradeable": summary.ungradeable,
        "attempted": summary.runs,
        "failed_by_fault": dict(sorted(summary.faults.items())),
        "synthetic_calls": summary.synthetic_calls,
        "graded": graded,
        "rate": summary.passed / graded if graded else 0.0,
        "interval": list(wilson_interval(summary.passed, graded)),
        "scenarios": [
            {
                "scenario": name,
                "passed": passed,
                "graded": total,
                "ungraded": scenario_ungraded.get(name, 0),
                "interval": list(wilson_interval(passed, total)),
            }
            for name, (passed, total) in sorted(summary.by_scenario.items())
        ],
        "chains": [
            {"chain": name, "passed": c[0], "graded": c[1], "proved_nothing": c[2]}
            for name, c in sorted(summary.chains.items())
        ],
        "chain_failures": list(summary.chain_failures),
        "failure_groups": sorted(
            groups.values(), key=lambda g: (-g["count"], g["fault"], g["last_step"])
        ),
        "not_graded": not_graded,
        "fields": [
            {"field": name, **asdict(tally), "disagreements": disagreements.get(name, [])}
            for name, tally in sorted(summary.fields.items())
        ],
        "not_run": [
            {"scenario_file": file, "reason": reason, "count": count}
            for (file, reason), count in sorted(not_run.items())
        ],
        "baseline": baseline.to_json(),
        "comparison": None,
    }
    if previous is not None:
        changes = compare(previous, summary)
        both = [c for c in changes if c.kind == "scenario" and c.before and c.after]
        card["comparison"] = {
            "previous_created": previous.created,
            "previous_source": previous.source,
            # Only scenarios run both times: a like-for-like total.
            "before": [sum(c.before[0] for c in both if c.before), _graded(both, "before")],
            "now": [sum(c.after[0] for c in both if c.after), _graded(both, "after")],
            "regressions": sum(c.regressed for c in changes),
            "beyond_noise": sum(c.beyond_noise for c in changes),
            "changes": [
                {
                    "kind": c.kind,
                    "name": c.name,
                    "before": list(c.before) if c.before else None,
                    "now": list(c.after) if c.after else None,
                    "verdict": c.verdict,
                }
                for c in changes
            ],
        }
    return card


def _graded(changes: list[Any], side: str) -> int:
    return sum(getattr(c, side)[1] for c in changes)


def load_previous(path: str | Path) -> Baseline:
    """An earlier result to compare with: a scorecard page, its ``.json``, or a baseline file."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json":
        data = json.loads(text)
    else:
        found = _DATA.search(text)
        if not found:
            raise ValueError(f"{path} is not a scorecard page: it carries no scorecard data")
        data = json.loads(found.group(1))
    if isinstance(data, dict) and isinstance(data.get("baseline"), dict):
        data = data["baseline"]  # a scorecard; a bare baseline file has no such key
    if not isinstance(data, dict) or "scenarios" not in data:
        raise ValueError(f"{path} holds neither a scorecard nor a baseline")
    return Baseline(
        created=str(data.get("created", "")),
        source=str(data.get("source", "")),
        scenarios={str(k): [int(v[0]), int(v[1])] for k, v in data["scenarios"].items()},
        chains={str(k): [int(v[0]), int(v[1])] for k, v in (data.get("chains") or {}).items()},
    )


def load_faults(path: str | Path) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("a faults file is one object: run -> fault class")
    return {str(k): str(v) for k, v in data.items()}


# --- the page -------------------------------------------------------------------------

_STYLE = """
:root { --bg:#fbfaf8; --fg:#1c2430; --muted:#5b6675; --line:#d9dee5; --card:#ffffff;
  --head:#0d54a0; --ok:#1c7c4a; --bad:#a3301f; --warn:#8a5a00; --bar:#e6eaf0; }
@media (prefers-color-scheme: dark) { :root { --bg:#12161c; --fg:#e7ebf0; --muted:#9aa6b4;
  --line:#2b3442; --card:#19202a; --head:#7fb4ee; --ok:#5ed39a; --bad:#f09a86; --warn:#e6b450;
  --bar:#263040; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui, "Segoe UI", sans-serif; }
main { max-width:1040px; margin:0 auto; padding:24px 16px 56px; }
h1 { font-size:26px; margin:0 0 4px; }
h2 { font-size:18px; margin:32px 0 8px; color:var(--head); }
.meta { color:var(--muted); margin:0 0 16px; }
.headline { font-size:20px; margin:0 0 4px; }
.headline strong { font-size:34px; }
.wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line);
  border-radius:8px; }
table { border-collapse:collapse; width:100%; min-width:640px; }
th, td { text-align:left; vertical-align:top; padding:8px 10px;
  border-bottom:1px solid var(--line); }
th { font-weight:600; color:var(--muted); font-size:13px; }
tr:last-child td { border-bottom:0; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
.ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.warn { color:var(--warn); font-weight:600; } .muted { color:var(--muted); }
.range { position:relative; height:10px; min-width:140px; background:var(--bar);
  border-radius:5px; }
.range i { position:absolute; top:0; bottom:0; background:var(--head); opacity:.45;
  border-radius:5px; }
.range b { position:absolute; top:-3px; bottom:-3px; width:3px; background:var(--fg);
  border-radius:2px; }
code { font:13px ui-monospace, Consolas, monospace; }
"""

_VERDICT_CLASS = {
    "regressed": "bad",
    "regressed (within noise)": "warn",
    "improved": "ok",
    "improved (within noise)": "ok",
    "missing": "warn",
}
_FAULT_CLASS = {"agent": "bad", "unclassified": "warn"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _span(css: str, text: Any) -> str:
    return f'<span class="{css}">{_e(text)}</span>'


def _range(passed: int, graded: int, interval: list[float]) -> str:
    if not graded:
        return _span("muted", "not graded")
    low, high = (round(x * 100, 1) for x in interval)
    at = round(passed / graded * 100, 1)
    label = f"{passed} of {graded}; 95% interval {low:.0f}% to {high:.0f}%"
    return (
        f'<div class="range" role="img" aria-label="{_e(label)}">'
        f'<i style="left:{low}%;width:{max(high - low, 1)}%"></i>'
        f'<b style="left:calc({at}% - 1px)"></b></div>'
    )


def _pair(value: list[int] | None) -> str:
    return f"{value[0]} of {value[1]}" if value else _span("muted", "-")


def _table(head: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    def cell(tag: str, index: int, inner: str) -> str:
        return f"<{tag}{' class=n' if head[index][1] else ''}>{inner}</{tag}>"

    top = "".join(cell("th", i, _e(text)) for i, (text, _) in enumerate(head))
    body = "".join(
        "<tr>" + "".join(cell("td", i, inner) for i, inner in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f'<div class="wrap"><table><tr>{top}</tr>{body}</table></div>'


def _comparison(card: dict[str, Any]) -> list[str]:
    change = card["comparison"]
    before, now = change["before"], change["now"]
    worse = change["regressions"]
    noise = (
        f"{change['beyond_noise']} beyond what chance explains"
        if change["beyond_noise"]
        else "none beyond what chance explains; repeat to be sure"
    )
    return [
        "<h2>Against the previous result</h2>",
        f"<p>Previous: {_e(change['previous_created'])} {_e(change['previous_source'])}. "
        f"Scenarios run both times: before {_e(describe(before[0], before[1]))}; "
        f"now {_e(describe(now[0], now[1]))}. "
        f"Got worse: {worse}" + (f" ({noise})." if worse else ".") + "</p>",
        _table(
            [("", False), ("Name", False), ("Before", True), ("Now", True), ("Verdict", False)],
            [
                [
                    _e(c["kind"]),
                    _e(c["name"]),
                    _pair(c["before"]),
                    _pair(c["now"]),
                    _span(_VERDICT_CLASS.get(c["verdict"], "muted"), c["verdict"]),
                ]
                for c in change["changes"]
            ],
        ),
    ]


def _fields(card: dict[str, Any]) -> list[str]:
    if not card["fields"]:
        return ['<p class="muted">No call carried a checked field.</p>']
    stored = sum(f["answered"] - f["not_stored"] for f in card["fields"])
    fetched = sum(f["read_back"] for f in card["fields"])
    parts = [
        _table(
            [
                ("Field", False),
                ("Answered", True),
                ("Stored correctly", True),
                ("Misheard", True),
                ("Stored wrongly", True),
                ("Not stored", True),
                ("Read back", True),
            ],
            [
                [_e(f["field"])]
                + [
                    str(f[k])
                    for k in (
                        "answered",
                        "stored_correctly",
                        "misheard",
                        "mis_stored",
                        "not_stored",
                        "read_back",
                    )
                ]
                for f in card["fields"]
            ],
        ),
        f'<p class="meta">"Stored" was read back from the system under test in {fetched} of '
        f"{stored} cases; the rest is what the agent's tool was sent.</p>",
    ]
    rows = [
        [
            _e(f["field"]),
            f"<code>{_e(d['run'])}</code>",
            _e(d["said"]),
            _e(d["heard"] if d["heard"] is not None else "-"),
            _e(d["stored"] if d["stored"] is not None else "-"),
            _e(d["stored_source"] or "-"),
        ]
        for f in card["fields"]
        for d in f["disagreements"]
    ]
    if rows:
        head = ["Field", "Run", "Said", "Heard", "Stored", "Stored taken from"]
        parts += ["<h2>Each disagreement</h2>", _table([(h, False) for h in head], rows)]
    return parts


def render_html(card: dict[str, Any]) -> str:
    graded = card["graded"]
    rate = f"{card['rate']:.0%}" if graded else "none graded"
    parts = [
        f"<h1>{_e(card['title'])}</h1>",
        f'<p class="meta">Generated {_e(card["generated"])} from '
        f"{_e(', '.join(card['sources']))}. Graded from each call's own tool results and "
        "stored values.</p>",
        f'<p class="headline"><strong>{_e(rate)}</strong> passed: '
        f"{_e(describe(card['passed'], graded))}</p>",
        f'<p class="meta">{card["calls"]} calls: {card["passed"]} passed, {card["failed"]} '
        f"failed, {card['ungraded']} not graded. Not run: "
        f"{sum(n['count'] for n in card['not_run'])}.</p>",
    ]
    if card.get("comparison"):
        parts += _comparison(card)

    parts.append("<h2>By scenario</h2>")
    parts.append(
        _table(
            [("Scenario", False), ("Passed", True), ("Not graded", True), ("95% interval", False)],
            [
                [
                    _e(s["scenario"]),
                    _span(
                        "ok" if s["passed"] == s["graded"] else "bad",
                        f"{s['passed']} of {s['graded']}",
                    )
                    if s["graded"]
                    else _span("muted", "-"),
                    str(s["ungraded"]),
                    _range(s["passed"], s["graded"], s["interval"]),
                ]
                for s in card["scenarios"]
            ],
        )
    )

    if card["chains"]:
        parts.append("<h2>Chains of calls, each graded as one</h2>")
        parts.append(
            _table(
                [("Chain", False), ("Passed", True), ("Proved nothing", True)],
                [
                    [_e(c["chain"]), f"{c['passed']} of {c['graded']}", str(c["proved_nothing"])]
                    for c in card["chains"]
                ],
            )
        )
        if card["chain_failures"]:
            items = "".join(f"<li>{_e(line)}</li>" for line in card["chain_failures"])
            parts.append(f"<ul>{items}</ul>")

    parts.append("<h2>Failures, by fault class and last step</h2>")
    if card["failure_groups"]:
        head = ["Fault", "Last step", "Calls", "Scenarios", "Failed checks", "Stopped at"]
        parts.append(
            _table(
                [(h, h == "Calls") for h in head],
                [
                    [
                        _span(_FAULT_CLASS.get(g["fault"], "muted"), g["fault"]),
                        f"<code>{_e(g['last_step'])}</code>",
                        str(g["count"]),
                        "<br>".join(_e(s) for s in g["scenarios"]),
                        "<br>".join(_e(c) for c in g["checks"]),
                        _e(g["stopped_at"]),
                    ]
                    for g in card["failure_groups"]
                ],
            )
        )
    else:
        parts.append('<p class="muted">No graded call failed.</p>')

    if card["not_graded"]:
        parts.append("<h2>Not graded</h2>")
        parts.append(
            _table(
                [("Run", False), ("Scenario", False), ("Fault", False), ("Why", False)],
                [
                    [
                        f"<code>{_e(n['run'])}</code>",
                        _e(n["scenario"]),
                        _e(n["fault"]),
                        _e(n["reason"]),
                    ]
                    for n in card["not_graded"]
                ],
            )
        )

    parts.append("<h2>Said, heard, stored</h2>")
    parts += _fields(card)

    if card["not_run"]:
        parts.append("<h2>Not run</h2>")
        parts.append(
            _table(
                [("Scenario file", False), ("Times", True), ("Reason", False)],
                [
                    [_e(n["scenario_file"]), str(n["count"]), _e(n["reason"])]
                    for n in card["not_run"]
                ],
            )
        )

    # "<" is escaped so nothing an agent said can close the data block.
    data = json.dumps(card, ensure_ascii=False).replace("<", "\\u003c")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(card['title'])}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        + "\n".join(parts)
        + "\n</main>\n"
        + f'<script type="application/json" id="scorecard-data">{data}</script>\n'
        + "</body>\n</html>\n"
    )


def write(card: dict[str, Any], out: str | Path) -> Path:
    """Write the page, and its numbers beside it as ``.json``."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(card), encoding="utf-8", newline="\n")
    write_json(out.with_suffix(".json"), card)
    return out
