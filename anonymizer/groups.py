"""Protect small groups (roadmap step 4).

Works on the output of step 3 plus the working columns of step 2, in four passes:

  A. Places.        Settlements, then communities, then regions with fewer than `min_group_size`
                    people are merged into a bucket ("Other settlements, X"). A bucket must hold
                    enough people from at least two places; otherwise the next-smallest neighbour
                    is pulled in, so nobody can find a hidden place by subtraction.
  B. Combinations.  Place x consumer type x voltage (the cookbook's columns) must also describe
                    enough people. If not: merge the categories, then show the place less precisely,
                    and as a last resort withhold the subscriber.
  C. Dominance.     If one member holds more than `dominance_limit` of a group's kWh, that member's
                    kWh is blanked (or only reported, per the cookbook).
  D. Release gate.  Everything is recounted from the finished table. One group below the minimum
                    raises GateError and nothing may be written.

People are counted, not rows or cards, and per month: a group's size is its smallest month.
Every decision is made once per subscriber, so a subscriber looks the same in every month.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .cookbook import Cookbook

LEVELS = ("settlement", "community", "region")       # narrowest to widest
PLURAL = {"settlement": "settlements", "community": "communities", "region": "regions"}


class GateError(RuntimeError):
    """A group below the minimum size survived. The run must write nothing."""

    def __init__(self, violations: list[tuple], report: "Report | None" = None):
        self.violations = violations          # (group, size) pairs, for code that needs to look closer
        self.report = report                  # what the run had done up to the gate
        smallest = min((size for _, size in violations), default=0)
        # The message carries counts only: the name of a too-small group is itself a raw value.
        super().__init__(f"release gate: {len(violations)} group(s) below the minimum size; "
                         f"the smallest has {smallest}")


@dataclass
class Report:
    minimum: int
    places_before: int = 0
    places_shown: int = 0
    buckets: list[dict] = field(default_factory=list)         # label, level, people, places (never per-place sizes)
    combination_steps: dict = field(default_factory=dict)      # what was done -> number of subscribers
    withheld: list[dict] = field(default_factory=list)         # reason, subscribers, rows
    dominance: list[dict] = field(default_factory=list)        # group, share, action
    substations_blanked_rows: int = 0
    also_blanked: dict = field(default_factory=dict)           # column -> rows where it was blanked by the wider check
    smallest_group: int = 0
    groups: int = 0
    gate: str = "not run"
    kept_rows: list = field(default_factory=list, repr=False)  # per input row: True if it is in the output

    def lines(self) -> list[str]:
        out = [f"places: {self.places_before} in the source, {self.places_shown} still shown by name"]
        for b in self.buckets:
            out.append(f"  bucket '{b['label']}': {b['people']} people from {b['places']} {PLURAL[b['level']]}")
        for what, count in self.combination_steps.items():
            out.append(f"combinations: {what}: {count} subscribers")
        for w in self.withheld:
            out.append(f"withheld: {w['subscribers']} subscribers ({w['rows']} rows): {w['reason']}")
        for d in self.dominance:
            out.append(f"dominance: one member held {d['share']:.0%} of the kWh in {d['group']} -> {d['action']}")
        out.append(f"substation blanked on {self.substations_blanked_rows} more rows (their settlement is no longer shown)")
        for column, rows in self.also_blanked.items():
            out.append(f"{column} blanked on {rows} rows: with place, type and voltage it described fewer than {self.minimum} people")
        out.append(f"release gate: {self.gate}; {self.groups} groups, smallest has {self.smallest_group} people")
        return out


class _Counter:
    """Counts distinct people in a set of cards, month by month, and returns the smallest month."""

    def __init__(self, cards: pd.Series, people: pd.Series, months: pd.Series):
        self.person_of = dict(zip(cards, people))
        self.present: dict[str, set] = {}
        for card, month in zip(cards, months):
            self.present.setdefault(month, set()).add(card)

    def size(self, cards) -> int:
        cards = set(cards)
        return min(len({self.person_of[c] for c in cards & there}) for there in self.present.values())


def _build_bucket(units: dict[str, set], counter: _Counter, minimum: int, unknown: str):
    """Decide which sibling units must be merged. Returns (keys in the bucket, kind).

    kind: None (nothing to do) | "other" (a real bucket beside visible siblings)
          | "whole" (everything merged: show the parent only) | "not_shown" (only blank values, too few)
    """
    sizes = {key: counter.size(cards) for key, cards in units.items()}
    bucket = sorted(key for key, size in sizes.items() if size < minimum)
    if not bucket:
        return [], None
    visible = sorted((key for key in units if key not in bucket), key=lambda key: (sizes[key], key))

    def valid() -> bool:
        return len(bucket) >= 2 and counter.size(set().union(*(units[k] for k in bucket))) >= minimum

    if not valid() and all(key == unknown for key in bucket):
        return bucket, "not_shown"           # a blank value is not a place: it never drags a named place in
    while not valid() and visible:
        bucket.append(visible.pop(0))        # pull in the next-smallest neighbour
    return bucket, ("other" if visible else "whole")


def protect(prepared: pd.DataFrame, output: pd.DataFrame, cookbook: Cookbook) -> tuple[pd.DataFrame, Report]:
    """Return (protected output, report). Raises GateError if a small group survives."""
    minimum = int(cookbook.parameters["min_group_size"])
    limit = float(cookbook.parameters["dominance_limit"])
    labels = cookbook.rollup_labels
    not_shown = labels["not_shown"]
    unknown = cookbook.unknown_label
    combo = cookbook.combination_check
    columns = list(combo.get("columns", []))
    merges: dict[str, dict] = combo.get("merge_first", {})
    card_column = cookbook.person_marker["fallback"]

    cards_per_row = prepared[card_column].astype(str).reset_index(drop=True)
    people_per_row = prepared["_person_key"].reset_index(drop=True)
    months_per_row = prepared["billing_month"].astype(str).reset_index(drop=True)
    counter = _Counter(cards_per_row, people_per_row, months_per_row)
    report = Report(minimum=minimum)

    # One line per card: where it is, and the columns of the combination check.
    first = ~cards_per_row.duplicated()
    keys = prepared["_location_key"].reset_index(drop=True)[first].str.split("|", expand=True)
    card = pd.DataFrame({
        "card": cards_per_row[first].tolist(),
        "region_key": keys[0].tolist(), "community_key": keys[1].tolist(), "settlement_key": keys[2].tolist(),
        "region": prepared["_region"].reset_index(drop=True)[first].tolist(),
        "community": prepared["_community"].reset_index(drop=True)[first].tolist(),
        "settlement": prepared["_settlement"].reset_index(drop=True)[first].tolist(),
        **{column: output[column].reset_index(drop=True)[first].astype(str).tolist() for column in columns},
    }, dtype=object).set_index("card")
    original_settlement = card["settlement"].copy()
    original_unit = {                                # each card's real place at every level, for the report
        "settlement": card["region_key"] + "|" + card["community_key"] + "|" + card["settlement_key"],
        "community": card["region_key"] + "|" + card["community_key"],
        "region": card["region_key"].copy(),
    }
    bucket_labels: dict[str, str] = {}               # label -> level it was made at
    report.places_before = card.groupby(["region_key", "community_key", "settlement_key"]).ngroups

    # ---------------------------------------------------------------- A. places
    def roll_up(level: str, parents: list[str], parent_name_column: str | None):
        """Merge small units of one level inside each parent."""
        key_column = f"{level}_key"
        grouped = card.groupby(parents, sort=True) if parents else [((), card)]
        for _, members in grouped:
            units = {key: set(group.index) for key, group in members.groupby(key_column)}
            bucket, kind = _build_bucket(units, counter, minimum, unknown)
            if kind is None:
                continue
            in_bucket = members.index[members[key_column].isin(bucket)]
            parent = members[parent_name_column].iloc[0] if parent_name_column else ""
            if kind == "not_shown":
                label = not_shown
            else:
                label = labels[level][kind].format(parent=parent)
                bucket_labels[label] = level
            # everything narrower than this level is merged too
            for narrower in LEVELS[: LEVELS.index(level) + 1]:
                card.loc[in_bucket, narrower] = label
            card.loc[in_bucket, key_column] = f"<{kind}>"      # the bucket is one unit from here on

    roll_up("settlement", ["region_key", "community_key"], "community")
    roll_up("community", ["region_key"], "region")
    roll_up("region", [], None)

    for label, level in bucket_labels.items():       # only buckets that survived the wider levels
        members = card.index[card[level] == label]
        if len(members):
            report.buckets.append({"label": label, "level": level, "people": counter.size(members),
                                   "places": int(original_unit[level][members].nunique())})

    # ---------------------------------------------------------------- B. combinations
    card["level"] = 0                    # 0 = as labelled above; 1, 2, 3 = settlement, community, region not shown
    card["withheld"] = False
    steps = {"categories merged": set(), "place shown less precisely": set()}
    merged: dict[str, set] = {}          # column -> cards whose value was merged

    def shown(frame: pd.DataFrame) -> pd.DataFrame:
        view = frame[["region", "community", "settlement", *columns]].copy()
        for depth, name in enumerate(LEVELS, start=1):
            view.loc[frame["level"] >= depth, name] = not_shown
        return view

    def small_cells(frame: pd.DataFrame) -> list[pd.Index]:
        view = shown(frame)
        return [members for _, members in view.groupby(list(view.columns)).groups.items()
                if counter.size(members) < minimum]

    while True:
        live = card[~card["withheld"]]
        cells = small_cells(live)
        if not cells:
            break
        view = shown(live)
        places = view[["region", "community", "settlement"]]

        # 1) first try merging categories, for everyone at the same place who is in the same merge group
        merged_something = False
        for members in cells:
            at_place = view.index[(places == places.loc[members[0]].to_numpy()).all(axis=1)]
            for column in columns:
                mapping = merges.get(column, {})
                for target in {mapping[v] for v in card.loc[members, column] if v in mapping}:
                    sources = [value for value, to in mapping.items() if to == target]
                    affected = at_place[card.loc[at_place, column].isin(sources)]
                    card.loc[affected, column] = target
                    merged.setdefault(column, set()).update(affected)
                    steps["categories merged"].update(affected)
                    merged_something = True
        if merged_something:
            continue                     # recount before doing anything stronger

        # 2) then show the place less precisely; 3) if even "nationwide" is too few, withhold
        for members in cells:
            can_rise = members[(card.loc[members, "level"] < len(LEVELS)).to_numpy()]
            if len(can_rise):
                card.loc[can_rise, "level"] += 1
                steps["place shown less precisely"].update(can_rise)
            else:
                card.loc[members, "withheld"] = True

    report.combination_steps = {what: len(cards) for what, cards in steps.items() if cards}

    # ---------------------------------------------------------------- write the decisions onto the rows
    final = shown(card)
    result = output.reset_index(drop=True).copy()
    for column in ["region", "community", "settlement", *columns]:
        result[column] = cards_per_row.map(final[column]).to_numpy()

    # A merged category must not be given away by another column (the tariff names the voltage).
    for column, followers in combo.get("follow", {}).items():
        rows_merged = cards_per_row.isin(merged.get(column, set())).to_numpy()
        for follower, mapping in followers.items():
            result.loc[rows_merged, follower] = result.loc[rows_merged, follower].replace(mapping)

    settlement_changed = cards_per_row.map(final["settlement"] != original_settlement).to_numpy()
    if "substation_id" in result.columns:
        blank_now = settlement_changed & (result["substation_id"] != "")
        result.loc[blank_now, "substation_id"] = ""
        report.substations_blanked_rows = int(blank_now.sum())

    keep = ~cards_per_row.map(card["withheld"]).to_numpy()
    if (~keep).any():
        report.withheld.append({"reason": "no group of enough similar subscribers exists, even nationwide",
                                "subscribers": int(card["withheld"].sum()), "rows": int((~keep).sum())})

    # ---------------------------------------------------------------- C. dominance
    kwh_columns = cookbook.columns_with("round_and_cap")
    kwh_outputs = [cookbook.rules[c].output for c in kwh_columns]
    group_columns = ["region", "community", "settlement", *columns]
    action = cookbook.dominance_action
    for column in kwh_outputs:
        result[column] = result[column].astype("Int64")
    live_rows = pd.Series(keep)
    total_kwh = result[kwh_outputs].sum(axis=1).astype(float)
    blanked: set[str] = set()
    while True:
        usable = live_rows & ~cards_per_row.isin(blanked)
        per_person = (pd.DataFrame({**{c: result[c] for c in group_columns}, "person": people_per_row,
                                    "card": cards_per_row, "kwh": total_kwh})[usable]
                      .groupby([*group_columns, "person"], as_index=False)["kwh"].sum())
        per_person["share"] = per_person["kwh"] / per_person.groupby(group_columns)["kwh"].transform("sum")
        worst = per_person[per_person["share"] > limit]
        if worst.empty:
            break
        for _, row in worst.iterrows():
            group = " / ".join(str(row[c]) for c in group_columns)
            report.dominance.append({"group": group, "share": float(row["share"]),
                                     "action": "kWh blanked" if action == "blank_kwh" else "reported only"})
            blanked.update(cards_per_row[people_per_row == row["person"]])
        if action != "blank_kwh":
            break
    if action == "blank_kwh" and blanked:
        result.loc[cards_per_row.isin(blanked).to_numpy(), kwh_outputs] = pd.NA

    result = result[keep].reset_index(drop=True)
    report.kept_rows = keep.tolist()
    people_kept = people_per_row[keep].reset_index(drop=True)
    months_kept = months_per_row[keep].reset_index(drop=True)

    # ---------------------------------------------------------------- C2. the other describing columns
    # A substation or a contract decade narrows a group down just as a small village does. Each is kept only
    # where place + type + voltage + the columns before it + itself still describe enough people.
    # Hiding one small value alone would let it be found by subtraction, so the hidden ones must add up to
    # enough people too: if they do not, the next-smallest value in that group is hidden with them.
    for column in combo.get("also_protect", []):
        report.also_blanked[column] = _keep_where_enough(result, people_kept, months_kept, group_columns, column, minimum)
        group_columns = [*group_columns, column]

    # ---------------------------------------------------------------- D. release gate
    violations, groups, smallest = gate(result, people_kept, months_kept, group_columns, minimum)
    still_named = final[(final["settlement"] == original_settlement) & ~card["withheld"]]
    report.places_shown = len(still_named.drop_duplicates(["region", "community", "settlement"]))
    report.groups, report.smallest_group = groups, smallest
    if violations:
        report.gate = "FAILED"
        raise GateError(violations, report)
    report.gate = "passed"
    return result, report


def _keep_where_enough(table: pd.DataFrame, people: pd.Series, months: pd.Series, cell_columns: list[str],
                       column: str, minimum: int) -> int:
    """Blank `column` (in place) wherever cell + column describes fewer than `minimum` people. Returns rows blanked."""
    before = int((table[column] != "").sum())
    while True:
        violations, _, _ = gate(table, people, months, [*cell_columns, column], minimum)
        if not violations:
            break
        changed = False
        in_cell = lambda cell: (table[cell_columns].astype(str) == list(cell)).all(axis=1)      # noqa: E731
        for cell, _ in violations:
            if len(cell) != len(cell_columns) + 1:         # not a cell of this table; leave it to the release gate
                continue
            *base, value = cell
            rows = in_cell(base)
            if value != "":                                # a small value: hide it
                table.loc[rows & (table[column] == value), column] = ""
                changed = True
                continue
            shown = table.loc[rows & (table[column] != ""), column]
            if shown.empty:                                # nothing left to hide here; the release gate decides
                continue
            sizes = people[shown.index].groupby(shown).nunique()
            table.loc[rows & (table[column] == sizes.idxmin()), column] = ""       # hide the next-smallest with them
            changed = True
        if not changed:
            break
    return before - int((table[column] != "").sum())


def gate(table: pd.DataFrame, people: pd.Series, months: pd.Series, group_columns: list[str],
         minimum: int) -> tuple[list[tuple], int, int]:
    """Recount every group from the finished table. Returns (violations, number of groups, smallest size).

    Independent of how the table was produced: it only looks at what is about to be written.
    """
    all_months = sorted(months.unique())
    frame = table[group_columns].astype(str).assign(_person=people.to_numpy(), _month=months.to_numpy())
    monthly = frame.groupby([*group_columns, "_month"])["_person"].nunique().unstack("_month")
    monthly = monthly.reindex(columns=all_months).fillna(0).astype(int)
    sizes = monthly.min(axis=1)
    violations = [(cell if isinstance(cell, tuple) else (cell,), int(size))
                  for cell, size in sizes.items() if size < minimum]
    return violations, len(sizes), int(sizes.min()) if len(sizes) else 0
