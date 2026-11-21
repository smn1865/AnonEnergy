"""The scoreboard (roadmap step 9): the 21 planted scenarios, and an attack on the tool's own output.

Each scenario from the Scenarios sheet is one check on the finished table. A check either passes or is
"open", and an open check always carries a written reason and the fix.

The attack asks, for one kind of outside knowledge at a time: how many subscribers does it single out
in the output? It uses what the tool knows and no attacker does (which output row is which person),
only to count the hits.

Everything here needs only Python and the mock file. Nothing is written or changed.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import groups, pipeline, prepare
from .cookbook import Cookbook

PLACE = ["region", "community", "settlement"]


@dataclass
class Result:
    id: str
    title: str
    passed: bool
    finding: str            # what was found, in one line
    reason: str = ""        # for an open check: why it is open
    fix: str = ""           # for an open check: what would close it
    known_limit: bool = False   # a limit of bill-level data itself: named on the scoreboard, shown only with --full


@dataclass
class Run:
    """One anonymization run, with the facts only the tool has."""
    cookbook: Cookbook
    source: pd.DataFrame            # every source row
    prepared: pd.DataFrame
    output: pd.DataFrame            # the finished table
    report: groups.Report
    kept: pd.DataFrame              # the source rows that are in the output, in output order
    kept_prepared: pd.DataFrame     # the same rows after location cleaning
    people: pd.Series               # the person behind each output row


def run(rows: pd.DataFrame, cookbook: Cookbook, hash_key: bytes) -> Run:
    prepared = prepare.prepare(rows, cookbook)
    output, report = pipeline.anonymize(rows, cookbook, hash_key)
    keep = pd.Series(report.kept_rows).to_numpy()
    return Run(cookbook, rows, prepared, output.reset_index(drop=True), report, rows[keep].reset_index(drop=True),
               prepared[keep].reset_index(drop=True), prepared["_person_key"][keep].reset_index(drop=True))


# ------------------------------------------------------------------ small helpers
def _bucket(r: Run, level: str, kind: str, parent: str = "") -> dict | None:
    label = r.cookbook.rollup_labels[level][kind].format(parent=parent)
    return next((b for b in r.report.buckets if b["label"] == label), None)


def _named(r: Run, column: str, names) -> list[str]:
    return sorted(set(r.output[column]) & set(names))


def _total_kwh(table: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(table["kwh_day"], errors="coerce").fillna(0) + pd.to_numeric(table["kwh_night"], errors="coerce").fillna(0)


def _singled_out(r: Run, columns: list[str], monthly: bool = False) -> int:
    """How many people are the only one in the output with their values in these columns."""
    by = [*columns, "billing_month"] if monthly else list(columns)
    table = r.output[by].astype(str).assign(_person=r.people)
    alone = table.groupby(by)["_person"].transform("nunique") == 1
    return int(table.loc[alone, "_person"].nunique())


def _open(result_id, title, finding, reason, fix) -> Result:
    return Result(result_id, title, False, finding, reason, fix)


UNLISTED = "The team's column list keeps every column it did not name, so this one is copied unchanged."


# ------------------------------------------------------------------ the 21 scenarios
def scenarios(r: Run) -> list[Result]:
    out, book, minimum = r.output, r.cookbook, int(r.cookbook.parameters["min_group_size"])
    results: list[Result] = []

    def add(result_id, title, passed, finding, reason="", fix=""):
        results.append(Result(result_id, title, bool(passed), finding, "" if passed else reason, "" if passed else fix))

    named = _named(r, "settlement", ["Կուրիս", "Հալիձոր", "Կեչուտ", "Հաղարծին"])
    add("S01", "Single-household settlement", not named, "none of the 4 one-household villages is named" if not named
        else f"{len(named)} one-household village(s) still named")

    named = _named(r, "settlement", ["Բանդիվան"])
    add("S02", "Small settlement and the row-count trap", not named, "Bandivan (2 subscribers, 6 rows) is not named: people are counted, not rows")

    bucket = _bucket(r, "settlement", "other", "Երևան")
    ok = not _named(r, "settlement", ["Նուբարաշեն"]) and bucket is not None and bucket["people"] >= minimum and bucket["places"] >= 2
    add("S03", "Only one small settlement in its community", ok,
        f"Nubarashen is hidden together with a neighbour: bucket of {bucket['people'] if bucket else 0} people from "
        f"{bucket['places'] if bucket else 0} districts, so it cannot be found by subtraction")

    small, whole = _bucket(r, "settlement", "other", "Ամասիա"), _bucket(r, "settlement", "whole", "Ամասիա")
    ok = small is None and whole is not None and whole["people"] >= minimum
    add("S04", "Merged bucket is still too small", ok,
        f"Amasia is shown only as a whole community of {whole['people'] if whole else 0}; no bucket of 4 was published")

    bucket = _bucket(r, "settlement", "other", "Աշոցք")
    add("S05", "Merged bucket reaches the minimum", bucket is not None and bucket["people"] >= minimum,
        f"'Other settlements, Ashotsk' holds {bucket['people'] if bucket else 0} people")

    bucket = _bucket(r, "community", "other", "Սյունիք")
    ok = not _named(r, "community", ["Մեղրի", "Տաթև"]) and bucket is not None and bucket["people"] >= minimum
    add("S06", "Whole community is too small", ok,
        f"Meghri and Tatev share 'Other communities, Syunik' ({bucket['people'] if bucket else 0} people)")

    bucket = _bucket(r, "region", "other")
    ok = not _named(r, "region", ["Վայոց ձոր", "Տավուշ"]) and bucket is not None and bucket["people"] >= minimum
    add("S07", "Whole region is too small", ok, f"Vayots Dzor and Tavush share 'Other regions' ({bucket['people'] if bucket else 0} people)")

    changes = int(out.groupby("subscriber_id")[[*PLACE, "consumer_type"]].nunique().max().max())
    ok = not _named(r, "settlement", ["Խնձորեսկ", "Ակներ"]) and changes == 1
    add("S08", "Group size crosses the threshold between months", ok,
        "Khndzoresk and Akner are hidden in every month; no subscriber changes group between months")

    columns = [*PLACE, *book.combination_check.get("columns", [])]
    every = [*columns, *book.combination_check.get("also_protect", [])]
    violations, cells, smallest = groups.gate(out, r.people, out["billing_month"], every, minimum)
    add("S09", "Small cell in a combination", not violations,
        f"all {cells} cells of place x {' x '.join(c.replace('_', ' ') for c in every[3:])} hold at least {smallest} people")

    per_person = pd.DataFrame({**{c: out[c] for c in columns}, "person": r.people, "kwh": _total_kwh(out)}) \
        .groupby([*columns, "person"], as_index=False)["kwh"].sum()
    share = (per_person["kwh"] / per_person.groupby(columns)["kwh"].transform("sum")).max()
    add("S10", "Count passes, but one member dominates", share <= float(book.parameters["dominance_limit"]),
        f"the biggest member of any group holds {share:.0%} of its kWh (limit {float(book.parameters['dominance_limit']):.0%})")

    social = int((out["tariff_category"] == "social").sum())
    add("S11", "Homogeneous group", social == 0, "no row carries the 'social' tariff; Verishen households look like any other")

    if "has_solar" not in out.columns:
        add("S12", "Unique attribute inside a large group", True, "the solar flag is not in the output")
    else:
        alone = _singled_out(r, [*columns, "has_solar"])
        add("S12", "Unique attribute inside a large group", alone == 0,
            f"the solar flag is published and singles out {alone} subscriber(s)", UNLISTED,
            "Set has_solar to 'delete' in cookbook.yaml and remove it from output_table.")

    resident_out = out["consumer_type"] == "resident"
    resident_in = r.source["consumer_type"] == "resident"
    per_home = _total_kwh(r.source)[resident_in].groupby(r.source["subscriber_card_no"][resident_in]).max().sort_values()
    largest_out = float(_total_kwh(out)[resident_out].max())
    add("S13", "Extreme value inside a large group", largest_out <= 1.25 * float(per_home.iloc[-2]),
        f"the largest household bill is {largest_out:,.0f} kWh in the output ({per_home.iloc[-1]:,.0f} in the source)")

    kwh_present = out["kwh_day"].notna() & out["kwh_night"].notna()
    zero = int(((_total_kwh(out) == 0) & kwh_present).sum())
    lowest = int(book.parameters.get("kwh_lowest_bin", 0))
    in_bin = (_total_kwh(out) <= lowest) & kwh_present
    add("S14", "Zero consumption", zero == 0,
        f"no bill of 0 kWh is published; bills of {lowest} kWh or less share one bin ({int(in_bin.sum())} bills, "
        f"{r.people[in_bin].nunique()} subscribers)" if zero == 0 else
        f"{zero} bill(s) of exactly 0 kWh are published: the home shows as empty",
        "No lowest bin is set, so 0 stays 0.", "Set kwh_lowest_bin in cookbook.yaml (for example 50).")

    add("S15", "One person, several cards", not _named(r, "settlement", ["Քարահունջ"]),
        "Karahunj (5 cards, 3 people) is not named: people are counted, not cards")

    add("S16", "Same settlement name in two regions", not _named(r, "settlement", ["Հարթաշեն"]),
        "the two Hartashens are counted separately (3 and 2), and neither is named")

    gyumri = out[out["community"] == "Գյումրի"]
    odd = [v for c in PLACE for v in set(out[c]) if v != v.strip() or "եւ" in v or v.startswith("ք.")]
    cards = r.prepared.loc[r.prepared["_community"] == "Գյումրի", "subscriber_card_no"].nunique()
    add("S17", "Spelling variants split a group", gyumri["settlement"].nunique() == 1 and not odd,
        f"the spellings of Gyumri are one place of {cards} subscribers; no variant spelling is left in the output")

    blank = int(out[PLACE].isin(["", book.unknown_label]).sum().sum())
    dropped = len(r.source) - len(out)
    reported = sum(w["rows"] for w in r.report.withheld)
    add("S18", "Missing location", blank == 0 and dropped == reported,
        f"no row has a blank or 'unknown' place; the {dropped} rows left out are all reported as withheld")

    shown = out[out["substation_id"] != ""] if "substation_id" in out.columns else out.iloc[0:0]
    serves = r.people[shown.index].groupby(shown["substation_id"]).nunique()
    hidden_rows = out["settlement"] != r.kept_prepared["_settlement"]        # rows whose settlement is no longer named
    leaks = int(hidden_rows[shown.index].sum())
    add("S19", "Substation ID works as a location", (serves.min() >= minimum if len(serves) else True) and leaks == 0,
        f"{len(serves)} substations are shown, each serving at least {int(serves.min()) if len(serves) else 0} people; "
        f"{leaks} row(s) of a hidden settlement carry one",
        "A substation is a location finer than the settlement, so a small one works like a small village.",
        "Blank every substation that serves fewer than the minimum among the people left in the output.")

    notes = [n for n in set(r.source["notes"]) if len(n) >= 8]
    everything = "\n".join(out.astype(object).where(out.notna(), "").astype(str).agg(" ".join, axis=1))
    found = [n for n in notes if n in everything]
    add("S20", "Personal data in free text", "notes" not in out.columns and not found,
        f"the notes column is gone, and none of its {len(notes)} texts appears anywhere in the output")

    business = r.source[r.source["consumer_type"] != "resident"]
    personal = [v for c in ("company_name", "first_name", "last_name", "phone", "taxpayer_no", "passport_or_id_no")
                for v in set(business[c]) if len(v) >= 5 and v in everything]
    as_person = book.person_marker.get("sole_proprietor") == book.person_marker.get("resident")
    add("S21", "Personal data in business rows", not personal and as_person,
        "no director's name, company name or phone is left; sole proprietors are counted as persons")
    return results


# ------------------------------------------------------------------ the attack on our own output
def attack(r: Run) -> list[Result]:
    """For each kind of outside knowledge: how many subscribers does it single out?"""
    out, total = r.output, int(r.people.nunique())
    base = [*PLACE, *r.cookbook.combination_check.get("columns", [])]
    results: list[Result] = []

    def add(result_id, knowledge, hits, reason="", fix=""):
        results.append(Result(result_id, knowledge, hits == 0, f"{hits} of {total}", "" if hits == 0 else reason,
                              "" if hits == 0 else fix))

    add("K01", "Where they live, the consumer type and the voltage", _singled_out(r, base))

    outside = "It is not part of the group check, so it narrows a group down."
    protect = "Add it to small_groups.also_protect in cookbook.yaml, or delete it."
    extras = [("substation_id", "which substation feeds them", False, outside, protect),
              ("contract_decade", "the decade the contract was signed", False, outside, protect),
              ("contract_year", "the year the contract was signed", False,
               "Only the year is kept, as the agreed list says, but the year is not part of the group check.",
               "Publish 10-year bands instead of the year and add them to also_protect, or delete the column."),
              ("has_solar", "whether they have solar panels", False, UNLISTED, "Delete has_solar."),
              ("meter_type", "the type of meter", False, UNLISTED, "Delete meter_type."),
              ("payment_channel", "how they paid in one month", True, UNLISTED, "Delete payment_channel."),
              ("reading_date", "the day the meter was read in one month", True, UNLISTED,
               "Delete reading_date; billing_month already gives the period.")]
    number, together = 2, []
    for column, words, monthly, reason, fix in extras:
        if column in out.columns:
            add(f"K{number:02d}", f"The same, and {words}", _singled_out(r, [*base, column], monthly), reason, fix)
            number += 1
            if not monthly:
                together.append(column)
    if len(together) > 1:
        add(f"K{number:02d}", "All of the above at once", _singled_out(r, [*base, *together]),
            "Each column passes alone, but together they narrow a group down.", "Put them all in also_protect; the gate then checks them together.")
        number += 1

    if "bill_id" in out.columns and (out["bill_id"] == r.kept["bill_id"]).any():
        add(f"K{number:02d}", "A bill number, as printed on any bill or receipt", _singled_out(r, ["bill_id"]),
            UNLISTED + " Anyone holding one bill or receipt finds that subscriber's rows.",
            "Set bill_id to 'keyed_hash' in cookbook.yaml (or delete it).")
        number += 1

    if "amount_paid_amd" in out.columns:
        add(f"K{number:02d}", "The amount paid in one month, from a receipt or a bank statement",
            _singled_out(r, ["amount_paid_amd"], monthly=True),
            UNLISTED + " The exact amount is almost unique, and it also gives away the capped household's real consumption.",
            "Delete amount_paid_amd (the agreed list already deletes the amount charged).")
        number += 1

    # Someone who has seen one paper bill knows the true kWh. The output has noise and rounding on it.
    noise = float(r.cookbook.parameters.get("kwh_noise_percent", 0)) / 100
    slack = int(r.cookbook.parameters["kwh_round_to"]) / 2
    day_out, night_out = pd.to_numeric(out["kwh_day"], errors="coerce"), pd.to_numeric(out["kwh_night"], errors="coerce")
    day_true, night_true = pd.to_numeric(r.kept["kwh_day"]), pd.to_numeric(r.kept["kwh_night"])
    place = out[PLACE].astype(str).agg("|".join, axis=1) + "|" + out["billing_month"].astype(str)
    found = set()
    for i in range(len(out)):
        near = ((place == place[i]) & ((day_out - day_true[i]).abs() <= day_true[i] * noise + slack)
                & ((night_out - night_true[i]).abs() <= night_true[i] * noise + slack)).fillna(False)
        if r.people[near].nunique() == 1 and (r.people[near] == r.people[i]).all():
            found.add(r.people[i])
    add(f"K{number:02d}", "Where they live and their true kWh in one month, from a paper bill", len(found),
        f"The noise is +/-{noise:.0%} and the rounding is to {int(slack * 2)}, so one known bill still points at one row. "
        "Whoever knows one month then learns the other months.",
        "This is the price of publishing bill-level rows. Stronger noise, wider bins, or totals per group instead of rows reduce it.")
    results[-1].known_limit = True
    return results


LIMIT_NOTE = ("Known limit, not in this table: someone who already knows a household's true kWh for one month, "
              "from a paper bill, can find that household's row. This comes with publishing bill-level rows.")


def _shown(attack_results: list[Result], full: bool) -> list[Result]:
    return list(attack_results) if full else [r for r in attack_results if not r.known_limit]


def score(results: list[Result]) -> tuple[int, int]:
    return sum(1 for r in results if r.passed), len(results)


# ------------------------------------------------------------------ showing the scoreboard
def text(scenario_results: list[Result], attack_results: list[Result], access_results: list[Result] | None,
         full: bool = False) -> list[str]:
    hidden = len(attack_results) - len(_shown(attack_results, full))
    attack_results = _shown(attack_results, full)
    lines = ["SCENARIOS"]
    for r in scenario_results:
        lines.append(f"  {r.id}  {'PASS' if r.passed else 'OPEN'}  {r.title}: {r.finding}")
        if not r.passed:
            lines += [f"             Why: {r.reason}", f"             Fix: {r.fix}"]
    lines.append("ACCESS")
    if access_results is None:
        lines.append("  not checked (run 'python score.py --db' to include the three access checks)")
    for r in access_results or []:
        lines.append(f"  {r.id}  {'PASS' if r.passed else 'OPEN'}  {r.title}: {r.finding}")
    lines.append("ATTACK ON OUR OWN OUTPUT: if someone knows this about a subscriber, how many can they single out?")
    for r in attack_results:
        lines.append(f"  {r.id}  {'HOLDS' if r.passed else 'OPEN '}  {r.finding:>10}  {r.title}")
        if not r.passed:
            lines += [f"             Why: {r.reason}", f"             Fix: {r.fix}"]
    if hidden:
        lines.append(f"  {LIMIT_NOTE}")
        lines.append("  To measure it:  python score.py --full")
    passed, total = score(scenario_results)
    held, kinds = score(attack_results)
    lines.append("")
    lines.append(f"SCORE: {passed} of {total} scenarios pass"
                 + ("" if access_results is None else f"; {score(access_results)[0]} of {len(access_results)} access checks pass")
                 + f"; the output holds against {held} of {kinds} kinds of outside knowledge")
    opened = [r.id for r in [*scenario_results, *(access_results or []), *attack_results] if not r.passed]
    lines.append("Open items, each with its reason and its fix written next to it: " + (", ".join(opened) or "none"))
    return lines


def html(scenario_results: list[Result], attack_results: list[Result], access_results: list[Result] | None,
         full: bool = False) -> str:
    """The scoreboard as one self-contained page."""
    hidden = len(attack_results) - len(_shown(attack_results, full))
    attack_results = _shown(attack_results, full)
    limit_note = f'<p class="note">{LIMIT_NOTE}</p>' if hidden else ""
    from html import escape as e

    from .report import CSS

    def mark(passed: bool, yes: str = "pass", no: str = "open") -> str:
        return (f'<span class="mark ok"><i aria-hidden="true">✓</i>{yes}</span>' if passed else
                f'<span class="mark stop"><i aria-hidden="true">✕</i>{no}</span>')

    passed, total = score(scenario_results)
    held, kinds = score(attack_results)
    scenario_rows = "".join(f'<tr><td class="id">{e(r.id)}</td><td title="{e(r.finding)}">{e(r.title)}</td>'
                            f'<td class="n">{mark(r.passed)}</td></tr>' for r in scenario_results)
    if access_results is None:
        access_block = '<p class="note">Not checked in this run. Use <code>python score.py --db</code>.</p>'
    else:
        access_block = "<table><tbody>" + "".join(
            f'<tr><td class="id">{e(r.id)}</td><td title="{e(r.finding)}">{e(r.title)}</td><td class="n">{mark(r.passed)}</td></tr>'
            for r in access_results) + "</tbody></table>"
    attack_rows = ""
    for r in attack_results:
        hits, people = (int(part) for part in r.finding.split(" of "))
        width = 100 * hits / people if people else 0
        attack_rows += (f'<tr><td>{e(r.title)}</td><td class="n">{e(r.finding)}</td>'
                        f'<td class="bar"><div class="track {"a" if r.passed else "b"}" title="{width:.0f}%">'
                        f'<i style="width:{width:.1f}%"></i></div></td><td class="n">{mark(r.passed, "holds")}</td></tr>')
    opened = [r for r in [*scenario_results, *(access_results or []), *attack_results] if not r.passed]
    open_block = "".join(f'<div class="open"><strong>{e(r.id)} · {e(r.title)}</strong> ({e(r.finding)})'
                         f'<div><em>Why:</em> {e(r.reason)}</div><div><em>Fix:</em> {e(r.fix)}</div></div>' for r in opened)
    access_score = "" if access_results is None else f" · access {score(access_results)[0]} of {len(access_results)}"
    extra = """
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:14px;align-items:start}
@media (max-width:900px){.grid{grid-template-columns:1fr}}
.stack{display:grid;gap:14px}
td{padding:3px 0}td.id{color:var(--ink2);width:42px;font-variant-numeric:tabular-nums}
td.bar{width:110px;padding-left:12px}
.mark{display:inline-flex;align-items:center;gap:6px;font-size:12px}
.mark i{font-style:normal;width:16px;height:16px;border-radius:50%;display:grid;place-items:center;color:#fff;font-size:10px}
.mark.ok i{background:var(--good)}.mark.stop i{background:var(--bad)}
.score{font-size:56px;font-weight:650;line-height:1}
.score small{font-size:16px;font-weight:400;color:var(--ink2);margin-left:8px}
.open{padding:8px 0;border-top:1px solid var(--line);font-size:13px}.open em{color:var(--ink2);font-style:normal}
"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anonymization scoreboard</title><style>{CSS}{extra}</style></head>
<body><main>
  <header>
    <div><h1>Scoreboard: 21 planted scenarios and an attack on our own output</h1>
      <div class="sub">mock data · an open item would be listed below with its reason and fix</div></div>
  </header>
  <div class="grid">
    <section class="card"><h2>The 21 scenarios</h2><table><tbody>{scenario_rows}</tbody></table></section>
    <div class="stack">
      <section class="card"><h2>Scenarios passed</h2>
        <div class="score">{passed}<small>of {total}{access_score} · output holds against {held} of {kinds} kinds of outside knowledge</small></div></section>
      <section class="card"><h2>Access</h2>{access_block}</section>
      <section class="card"><h2>Attack: if someone knows this about a subscriber, how many can they single out?</h2>
        <table><tbody>{attack_rows}</tbody></table>{limit_note}</section>
    </div>
  </div>
  <section class="card"><h2>Open items: why, and what closes them</h2>{open_block or '<p class="note">None.</p>'}</section>
</main></body></html>
"""
