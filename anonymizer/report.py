"""The risk report (roadmap step 8): what a run did, in numbers, and whether it may be released.

build_record() describes one run: rows in and out, what happened to the columns, which places were rolled
up, the release gate, who ran it and when. The same record goes into the audit log (audit.py) and is drawn
as a one-screen page (html()) and as a few terminal lines (text()).

The headline measure is the share of subscribers who sit in a group smaller than the minimum size when
grouped by place, consumer type and tariff group. It is counted on the source rows and again on the output.

A record holds counts, column names and published labels only: never a raw value from the source, never
the name or the size of a place that was hidden.
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import html as html_module
import platform
from pathlib import Path

import pandas as pd

from . import groups, prepare
from .cookbook import Cookbook

PLACE = ["region", "community", "settlement"]
CHANGING_ACTIONS = {"year_only", "clean_location", "keep_if_min_group", "merge_values", "round_and_cap"}
MAX_BUCKETS_ON_SCREEN = 6


# ------------------------------------------------------------------ the headline measure
def measure(table: pd.DataFrame, people: pd.Series, months: pd.Series, columns: list[str], minimum: int,
            merges: dict | None = None) -> dict:
    """How many subscribers sit in a group smaller than `minimum`, grouped by place and `columns`.

    Groups are counted the way the release gate counts them: distinct people, in the group's smallest month.
    """
    by = [*PLACE, *columns]
    frame = table[by].astype(str).reset_index(drop=True)
    for column, mapping in (merges or {}).items():
        frame[column] = frame[column].replace(mapping)
    people, months = people.reset_index(drop=True), months.reset_index(drop=True)
    violations, group_count, smallest = groups.gate(frame, people, months, by, minimum)
    small = {cell for cell, _ in violations}
    in_small = [tuple(row) in small for row in frame.itertuples(index=False, name=None)]
    exposed, total = int(people[in_small].nunique()), int(people.nunique())
    return {"subscribers": total, "in_small_groups": exposed,
            "percent": round(100 * exposed / total, 1) if total else 0.0,
            "groups": group_count, "smallest_group": smallest}


def _measures(rows: pd.DataFrame, cookbook: Cookbook, protected: pd.DataFrame | None, kept_rows) -> dict:
    settings = cookbook.risk_report
    columns, merges = list(settings["measure_columns"]), dict(settings["merge_for_measure"])
    minimum = int(cookbook.parameters["min_group_size"])
    prepared = prepare.prepare(rows, cookbook)
    month_source = cookbook.columns_with("keep_time")[0]
    before_table = pd.DataFrame({"region": prepared["_region"], "community": prepared["_community"],
                                 "settlement": prepared["_settlement"], **{c: prepared[c] for c in columns}})
    people, months = prepared["_person_key"], prepared[month_source]
    names = [c.replace("_category", "_group" if c in merges else "_category").replace("_", " ") for c in columns]
    result = {"definition": f"share of subscribers in a group smaller than {minimum} by place, {' and '.join(names)}",
              "minimum": minimum, "before": measure(before_table, people, months, columns, minimum, merges), "after": None}
    banded = {"before": measure(before_table, people, months, columns, minimum), "after": None}
    if protected is not None:
        people_after = people[pd.Series(kept_rows).to_numpy()]
        months_after = protected[cookbook.rules[month_source].output]
        result["after"] = measure(protected, people_after, months_after, columns, minimum, merges)
        banded["after"] = measure(protected, people_after, months_after, columns, minimum)
    if merges:                                           # the stricter count, kept on record for honesty
        result["without_merging_bands"] = banded
    return result


# ------------------------------------------------------------------ the record of one run
def _who() -> dict:
    try:
        user = getpass.getuser()
    except Exception:                                    # noqa: BLE001 - no user name on this system
        user = "unknown"
    return {"user": user, "machine": platform.node() or "unknown"}


def _columns(cookbook: Cookbook) -> dict:
    by_action = lambda wanted: [c for c, r in cookbook.rules.items() if r.action in wanted]     # noqa: E731
    return {"in": len(cookbook.rules), "out": len(cookbook.output_columns), "deleted": by_action({"delete"}),
            "hashed": by_action({"keyed_hash"}), "changed": by_action(CHANGING_ACTIONS),
            "unchanged": by_action({"keep", "keep_time"})}


def build_record(command: str, rows: pd.DataFrame, cookbook: Cookbook, cookbook_path: str | Path, *, source: str,
                 destination: str, keys: dict, protected: pd.DataFrame | None = None,
                 group_report: groups.Report | None = None, error: Exception | None = None,
                 written: bool = False) -> dict:
    """Describe one run. Pass the finished table and its report, or the error that stopped the run."""
    if error is not None:
        protected, written = None, False
        group_report = getattr(error, "report", None) or group_report
    minimum = int(cookbook.parameters["min_group_size"])
    card_column = cookbook.person_marker["fallback"]

    gate = {"result": "not reached", "groups": None, "smallest_group": None, "reason": None}
    if group_report is not None:
        gate.update(result=group_report.gate, groups=group_report.groups, smallest_group=group_report.smallest_group)
    stopped_because = None
    if isinstance(error, groups.GateError):
        smallest = min((size for _, size in error.violations), default=0)
        gate.update(result="FAILED", reason=f"{len(error.violations)} group(s) below {minimum} people; the smallest has {smallest}")
        stopped_because = "release gate: " + gate["reason"]
    elif error is not None:                              # the leak scan: column names only, never the values found
        found = ", ".join(getattr(error, "columns", [])) or type(error).__name__
        stopped_because = f"leak scan: raw values found from {found}"

    places = {"in": None, "named": None, "buckets": []}
    withheld = 0
    if group_report is not None:
        places = {"in": group_report.places_before, "named": group_report.places_shown,
                  "buckets": [{"label": b["label"], "people": b["people"], "merged_places": b["places"],
                               "level": b["level"]} for b in group_report.buckets]}
        withheld = sum(w["subscribers"] for w in group_report.withheld)

    measures = _measures(rows, cookbook, protected, group_report.kept_rows if group_report is not None else None)
    return {
        "at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "who": _who(),
        "command": command,
        "source": source,
        "destination": destination,
        "cookbook": {"version": cookbook.version,
                     "sha256": hashlib.sha256(Path(cookbook_path).read_bytes()).hexdigest()[:16]},
        "keys": keys,                                    # key IDs, never the keys
        "outcome": "stopped" if error is not None else "passed",
        "stopped_because": stopped_because,
        "written": bool(written),
        "rows": {"in": len(rows), "out": None if protected is None else len(protected)},
        "cards": {"in": int(rows[card_column].nunique()),
                  "out": None if protected is None else int(protected[cookbook.rules[card_column].output].nunique()),
                  "withheld": withheld},
        "columns": _columns(cookbook),
        "places": places,
        "gate": gate,
        "leak_scan": "not reached" if isinstance(error, groups.GateError) else
                     ("raw values found" if error is not None else "clean"),
        "measure": measures,
    }


# ------------------------------------------------------------------ showing a record
def _percent(value: float | None) -> str:
    if value is None:
        return "-"
    return "0%" if value == 0 else f"{value:.1f}%"


def _of(part: dict | None) -> str:
    return "not released" if part is None else f"{part['in_small_groups']} of {part['subscribers']} subscribers"


def _arrow(pair: dict, arrow: str = "→") -> str:
    return f"{pair['in']} {arrow} {'-' if pair['out'] is None else pair['out']}"


def text(record: dict) -> list[str]:
    """The report as a few terminal lines."""
    m, columns, places = record["measure"], record["columns"], record["places"]
    passed = record["outcome"] == "passed"
    lines = [f"RISK REPORT: {'PASSED' if passed else 'STOPPED - ' + record['stopped_because']}",
             f"  {m['definition']}:",
             f"    before  {_percent(m['before']['percent']):>6}   ({_of(m['before'])})",
             f"    after   {_percent(m['after']['percent'] if m['after'] else None):>6}   ({_of(m['after'])})",
             f"  rows {_arrow(record['rows'], '->')}; columns {columns['in']} -> {columns['out']} "
             f"({len(columns['deleted'])} deleted, {len(columns['hashed'])} hashed, {len(columns['changed'])} changed)"]
    if places["in"] is not None:
        lines.append(f"  places {places['in']} -> {places['named']} named + {len(places['buckets'])} buckets; "
                     f"smallest group {record['gate']['smallest_group']} (minimum {m['minimum']}); "
                     f"{record['cards']['withheld']} subscriber(s) withheld")
    return lines


CSS = """
:root{--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;
--ring:rgba(11,11,11,.10);--good:#0ca30c;--bad:#d03b3b;--good-track:rgba(12,163,12,.16);--bad-track:rgba(208,59,59,.16)}
@media (prefers-color-scheme:dark){:root{--plane:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--line:#2c2c2a;
--ring:rgba(255,255,255,.10);--good-track:rgba(12,163,12,.22);--bad-track:rgba(208,59,59,.24)}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 24px;display:grid;gap:14px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:20px;font-weight:650;margin:0}
.sub,.note,.label,th{color:var(--ink2)}
.sub{margin-top:2px}
.badge{display:flex;align-items:center;gap:10px;padding:8px 16px 8px 10px;border-radius:999px;background:var(--surface);
box-shadow:0 0 0 1px var(--ring);font-weight:650;font-size:16px;letter-spacing:.02em}
.dot{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;color:#fff;font-size:16px;line-height:1}
.ok .dot{background:var(--good)}.stop .dot{background:var(--bad)}
.badge small{display:block;font-weight:400;font-size:12px;letter-spacing:0;color:var(--ink2)}
.card{background:var(--surface);border-radius:12px;box-shadow:0 0 0 1px var(--ring);padding:16px 18px}
.top{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:14px}
.bottom{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:14px}
@media (max-width:820px){.top,.bottom{grid-template-columns:1fr}}
h2{font-size:13px;font-weight:600;margin:0 0 10px;color:var(--ink2)}
.pair{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap}
.figure .label{font-size:12px}
.before{font-size:40px;font-weight:600;line-height:1.05;color:var(--ink2)}
.after{font-size:68px;font-weight:650;line-height:1}
.to{font-size:30px;color:var(--muted);padding-bottom:22px}
.of{font-size:12px;color:var(--ink2);margin-top:4px}
.meters{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:center;margin-top:14px;font-size:12px;color:var(--ink2)}
.track{height:8px;border-radius:4px;overflow:hidden}
.track i{display:block;height:100%;border-radius:4px}
.track.b{background:var(--bad-track)}.track.b i{background:var(--bad)}
.track.a{background:var(--good-track)}.track.a i{background:var(--good)}
.note{font-size:12px;margin:12px 0 0}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
@media (max-width:520px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile{background:var(--surface);border-radius:12px;box-shadow:0 0 0 1px var(--ring);padding:12px 14px}
.tile .value{font-size:24px;font-weight:600;line-height:1.2;margin-top:2px}
.tile .label,.tile .hint{font-size:12px}
.tile .hint{color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-weight:500;font-size:12px;text-align:left;padding:0 0 6px}
td{padding:5px 0;border-top:1px solid var(--line)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;padding-left:12px;white-space:nowrap}
dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 14px;margin:0;font-size:13px}
dt{color:var(--ink2)}dd{margin:0;overflow-wrap:anywhere}
code{font:12px ui-monospace,Consolas,monospace}
.alert{border-left:4px solid var(--bad);padding:10px 14px;background:var(--surface);border-radius:8px;box-shadow:0 0 0 1px var(--ring)}
"""


def _tile(label: str, value: str, hint: str) -> str:
    e = html_module.escape
    return (f'<div class="tile"><div class="label">{e(label)}</div><div class="value">{e(value)}</div>'
            f'<div class="hint">{e(hint)}</div></div>')


def html(record: dict) -> str:
    """The report as one self-contained page that fits one screen."""
    e = html_module.escape
    m, columns, places, gate = record["measure"], record["columns"], record["places"], record["gate"]
    passed = record["outcome"] == "passed"
    before, after = m["before"], m["after"]
    after_percent = after["percent"] if after else None

    badge = ('<div class="badge ok"><span class="dot" aria-hidden="true">✓</span><span>PASSED<small>safe to release</small></span></div>'
             if passed else
             '<div class="badge stop"><span class="dot" aria-hidden="true">✕</span><span>STOPPED<small>nothing was written</small></span></div>')
    alert = "" if passed else f'<div class="alert"><strong>Why it stopped:</strong> {e(record["stopped_because"])}.</div>'

    after_meter = "" if after is None else (
        f'<span>after</span><div class="track a" title="{e(_percent(after_percent))}"><i style="width:{after_percent}%"></i></div>')
    hero = f"""
    <section class="card">
      <h2>Subscribers who could be singled out</h2>
      <div class="pair">
        <div class="figure"><div class="label">before</div><div class="before">{e(_percent(before['percent']))}</div>
          <div class="of">{e(_of(before))}</div></div>
        <div class="to" aria-hidden="true">→</div>
        <div class="figure"><div class="label">after</div><div class="after">{e(_percent(after_percent))}</div>
          <div class="of">{e(_of(after))}</div></div>
      </div>
      <div class="meters">
        <span>before</span><div class="track b" title="{e(_percent(before['percent']))}"><i style="width:{before['percent']}%"></i></div>
        {after_meter}
      </div>
      <p class="note">A subscriber counts when their group (the same place, consumer type and tariff group)
      has fewer than {m['minimum']} people. Lower is safer.</p>
    </section>"""

    named = "–" if places["named"] is None else f"{places['named']} of {places['in']}"
    tiles = "".join([
        _tile("Rows", _arrow(record["rows"]), "bills in → bills out"),
        _tile("Columns", f"{columns['in']} → {columns['out']}",
              f"{len(columns['deleted'])} deleted · {len(columns['hashed'])} hashed · {len(columns['changed'])} changed"),
        _tile("Places still named", named, f"the rest merged into {len(places['buckets'])} buckets"),
        _tile("Smallest group", "–" if gate["smallest_group"] is None else f"{gate['smallest_group']} people",
              f"minimum allowed is {m['minimum']}"),
        _tile("Withheld", str(record["cards"]["withheld"]), "subscribers with no safe group"),
        _tile("Leak scan", {"clean": "0 found", "raw values found": "FOUND"}.get(record["leak_scan"], "–"),
              "raw names, numbers, contacts"),
    ])

    shown = sorted(places["buckets"], key=lambda b: -b["people"])[:MAX_BUCKETS_ON_SCREEN]
    rows_html = "".join(f'<tr><td>{e(b["label"])}</td><td class="n">{b["people"]}</td>'
                        f'<td class="n">{b["merged_places"]} {e(groups.PLURAL[b["level"]])}</td></tr>' for b in shown)
    more = len(places["buckets"]) - len(shown)
    buckets = (f'<table><thead><tr><th>Bucket</th><th class="n">People</th><th class="n">Merged from</th></tr></thead>'
               f'<tbody>{rows_html}</tbody></table>' if shown else '<p class="note">No buckets in this run.</p>')
    if more > 0:
        buckets += f'<p class="note">and {more} smaller bucket(s); the audit log lists them all.</p>'

    keys = " · ".join(f"{e(name.replace('_', ' '))} <code>{e(str(value))}</code>" for name, value in record["keys"].items())
    log_line = (f'line {record["line"]} · <code>{e(record["hash"][:16])}</code>' if "hash" in record else "not written to the log")
    facts = f"""
      <dl>
        <dt>Run by</dt><dd>{e(record['who']['user'])} on {e(record['who']['machine'])}</dd>
        <dt>Command</dt><dd>{e(record['command'])}</dd>
        <dt>From</dt><dd>{e(record['source'])}</dd>
        <dt>To</dt><dd>{e(record['destination'])}{'' if record['written'] or not passed else ' (nothing saved)'}</dd>
        <dt>Cookbook</dt><dd>version {e(str(record['cookbook']['version']))} · <code>{e(record['cookbook']['sha256'])}</code></dd>
        <dt>Key IDs</dt><dd>{keys}</dd>
        <dt>Audit log</dt><dd>{log_line}</dd>
      </dl>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anonymization run report</title><style>{CSS}</style></head>
<body><main>
  <header>
    <div><h1>Anonymization run report</h1><div class="sub">{e(record['at'])} · release gate {e(gate['result'])}</div></div>
    {badge}
  </header>
  {alert}
  <div class="top">{hero}
    <div class="tiles">{tiles}</div>
  </div>
  <div class="bottom">
    <section class="card"><h2>Small places were merged into these buckets (totals only)</h2>{buckets}</section>
    <section class="card"><h2>This run</h2>{facts}</section>
  </div>
</main></body></html>
"""
