"""Load and validate the cookbook (roadmap step 1).

The cookbook holds rules only. This module refuses a cookbook that is
ambiguous, so a teammate can always predict the output table from the file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Every action a column rule may use. Steps 3 and 4 implement them.
ACTIONS = {
    "delete",             # never reaches the output
    "keep",               # copied unchanged
    "keep_time",          # copied; timestamps finer than `granularity` are truncated
    "keyed_hash",         # replaced with HMAC(secret key, value)
    "year_only",          # a date becomes its year
    "clean_location",     # normalized spelling; with rollup: true, small groups are merged in step 4
    "keep_if_min_group",  # kept only where it covers at least min_group_size subscribers
    "merge_values",       # listed values are merged into another category
    "round_and_cap",      # rounded to kwh_round_to and capped at kwh_cap_percentile
}
REQUIRED_PARAMETERS = ("min_group_size", "kwh_round_to", "kwh_cap_percentile", "dominance_limit")
# The risk report and the audit log (step 8).
RISK_REPORT_DEFAULTS = {"measure_columns": ["consumer_type", "tariff_category"], "merge_for_measure": {},
                        "audit_log": "audit_log.jsonl", "report_file": "report.html"}
# Limits on researcher accounts (step 7). The cookbook may change any of them.
RESEARCHER_DEFAULTS = {"account_days": 30, "daily_query_limit": 200, "query_seconds": 60, "connections": 3}


class CookbookError(ValueError):
    """The cookbook is missing something or contradicts itself."""


@dataclass(frozen=True)
class Rule:
    column: str
    action: str
    output: str | None   # name in the output table, or None when the column is deleted
    listed: bool         # on the team's agreed list (False = passed through or dropped by default)
    options: dict


@dataclass(frozen=True)
class Cookbook:
    version: int
    schema: str
    table: str
    parameters: dict
    rules: dict[str, Rule]            # source column -> rule, in source order
    output_table: str
    output_columns: list[str]
    location_levels: list[str]
    strip_prefixes: list[str]
    unknown_label: str
    person_marker: dict[str, str]
    on_unknown_column: str
    on_missing_column: str
    leak_scan_columns: list[str]
    rollup_labels: dict
    combination_check: dict
    dominance_action: str
    second_database: dict
    risk_report: dict = field(default_factory=dict)

    @property
    def source_columns(self) -> list[str]:
        return list(self.rules)

    def columns_with(self, action: str) -> list[str]:
        return [c for c, r in self.rules.items() if r.action == action]


def load(path: str | Path = "cookbook.yaml") -> Cookbook:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    problems: list[str] = []

    for key in ("version", "source", "parameters", "columns", "output_table", "location_cleaning", "person_marker"):
        if key not in raw:
            problems.append(f"missing top-level key: {key}")
    if problems:
        raise CookbookError("; ".join(problems))

    for p in REQUIRED_PARAMETERS:
        if p not in raw["parameters"]:
            problems.append(f"missing parameter: {p}")

    rules: dict[str, Rule] = {}
    for column, spec in raw["columns"].items():
        spec = dict(spec or {})
        action = spec.pop("action", None)
        if action not in ACTIONS:
            problems.append(f"{column}: unknown action {action!r}")
            continue
        output = None if action == "delete" else spec.pop("output", column)
        rules[column] = Rule(column, action, output, bool(spec.pop("listed", False)), spec)

    # The output table must be exactly what the rules produce: nothing extra, nothing lost.
    produced = [r.output for r in rules.values() if r.output]
    wanted = list(raw["output_table"]["columns"])
    for name in sorted(set(produced) - set(wanted)):
        problems.append(f"rule produces '{name}' but the output table does not list it")
    for name in sorted(set(wanted) - set(produced)):
        problems.append(f"output table lists '{name}' but no rule produces it")
    for name in sorted({n for n in produced if produced.count(n) > 1}):
        problems.append(f"two rules produce the same output column '{name}'")

    levels = list(raw["location_cleaning"]["levels"])
    for level in levels:
        if rules.get(level) is None or rules[level].action != "clean_location":
            problems.append(f"location level '{level}' needs a clean_location rule")
    for kind, column in raw["person_marker"].items():
        if column not in rules:
            problems.append(f"person_marker.{kind} points at unknown column '{column}'")

    groups = raw.get("small_groups", {})
    group_labels = groups.get("labels", {})
    for level in levels:
        for kind in ("other", "whole"):
            if kind not in group_labels.get(level, {}):
                problems.append(f"small_groups.labels.{level}.{kind} is missing")
    if "not_shown" not in group_labels:
        problems.append("small_groups.labels.not_shown is missing")
    combination = dict(groups.get("combination_check", {}))
    for column in combination.get("columns", []):
        if column not in wanted:
            problems.append(f"combination_check column '{column}' is not in the output table")
    for column in groups.get("also_protect", []):
        if column not in wanted:
            problems.append(f"small_groups.also_protect column '{column}' is not in the output table")
        elif column in combination.get("columns", []):
            problems.append(f"small_groups.also_protect: '{column}' is already a combination_check column")
    combination["also_protect"] = list(groups.get("also_protect", []))
    if groups.get("dominance_action", "blank_kwh") not in ("blank_kwh", "report_only"):
        problems.append("small_groups.dominance_action must be blank_kwh or report_only")

    second = dict(raw.get("second_database", {}))
    if "store_table" not in second:
        problems.append("second_database.store_table is missing")
    for column, sql_type in second.get("column_types", {}).items():
        if column not in wanted:
            problems.append(f"second_database.column_types names '{column}', which is not in the output table")
        if sql_type not in ("text", "integer", "bigint", "numeric", "date"):
            problems.append(f"second_database.column_types.{column}: unsupported type '{sql_type}'")

    limits = {**RESEARCHER_DEFAULTS, **dict(second.get("researchers") or {})}
    for name, value in limits.items():
        if name not in RESEARCHER_DEFAULTS:
            problems.append(f"second_database.researchers: unknown setting '{name}'")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"second_database.researchers.{name} must be a whole number of at least 1")
    second["researchers"] = limits

    risk = {**RISK_REPORT_DEFAULTS, **dict(raw.get("risk_report") or {})}
    for column in risk["measure_columns"]:
        if column not in wanted or column not in rules:
            problems.append(f"risk_report.measure_columns: '{column}' must be a column of both the source and the output table")
    for column in risk["merge_for_measure"]:
        if column not in risk["measure_columns"]:
            problems.append(f"risk_report.merge_for_measure names '{column}', which is not a measure column")

    leak_columns = list(raw.get("leak_scan_columns", []))
    for column in leak_columns:
        if column not in rules:
            problems.append(f"leak_scan_columns names unknown column '{column}'")
        elif rules[column].action not in ("delete", "keyed_hash"):
            problems.append(f"leak_scan_columns: '{column}' is kept, so its values are expected in the output")

    if problems:
        raise CookbookError("cookbook is not valid:\n  - " + "\n  - ".join(problems))

    return Cookbook(
        version=int(raw["version"]),
        schema=raw["source"]["schema"],
        table=raw["source"]["table"],
        parameters=dict(raw["parameters"]),
        rules=rules,
        output_table=raw["output_table"]["name"],
        output_columns=wanted,
        location_levels=levels,
        strip_prefixes=list(raw["location_cleaning"].get("strip_prefixes", [])),
        unknown_label=raw["location_cleaning"].get("unknown_label", "unknown"),
        person_marker=dict(raw["person_marker"]),
        on_unknown_column=raw.get("on_unknown_column", "drop_and_report"),
        on_missing_column=raw.get("on_missing_column", "fail"),
        leak_scan_columns=leak_columns,
        rollup_labels=group_labels,
        combination_check=combination,
        dominance_action=groups.get("dominance_action", "blank_kwh"),
        second_database=second,
        risk_report=risk,
    )
