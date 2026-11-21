"""Apply the cookbook's column rules (roadmap step 3).

Input:  the rows from step 2, with their working columns (_region, _person_key, ...).
Output: a table with exactly the cookbook's output columns, in order, and nothing else.

Settlement names are cleaned here but small settlements are not merged yet; that is step 4.
Nothing is written to a database here; that is step 5.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

import pandas as pd

from .cookbook import Cookbook

KEY_ENV = "ANON_HASH_KEY"
MIN_KEY_LENGTH = 32
_FINE_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):\d{2}")
_YEAR = re.compile(r"^(\d{4})")


class HashKeyError(RuntimeError):
    """The secret key for hashing is missing or too short."""


def load_key() -> bytes:
    """Read the hash key from the environment. It is never read from a file or the cookbook."""
    key = os.environ.get(KEY_ENV, "")
    if len(key) < MIN_KEY_LENGTH:
        raise HashKeyError(
            f"{KEY_ENV} is not set or is shorter than {MIN_KEY_LENGTH} characters. "
            "Create one with:  python -m anonymizer keygen"
        )
    return key.encode("utf-8")


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def keyed_hash(key: bytes, column: str, value: object, length: int = 16) -> str:
    """HMAC-SHA256 of the value. The column name is mixed in, so equal values in two columns differ."""
    text = _text(value)
    if not text:
        return ""
    message = f"{column}\x1f{text}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:length]


def unit_noise(key: bytes, column: str, card: object, period: object) -> float:
    """A number in [-1, 1) that only the key holder can reproduce, fixed per card, period and column.

    The same bill always gets the same noise, so running the tool again (or publishing the month twice)
    gives nobody a second sample to average the noise away with.
    """
    message = f"noise\x1f{column}\x1f{_text(card)}\x1f{_text(period)}".encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") / 2**63 - 1.0


def year_only(value: object, band_years: int = 0) -> str:
    """'2014-03-09' -> '2014', or with band_years=10 -> '2010-2019'."""
    match = _YEAR.match(_text(value))
    if not match:
        return ""
    if band_years and band_years > 1:
        start = int(match.group(1)) // band_years * band_years
        return f"{start}-{start + band_years - 1}"
    return match.group(1)


def bounded_kwh_noise(key: bytes, column: str, card: object, period: object,
                      bill: object, value: object, percent: float = 1.0) -> float:
    """Stable noise per bill and component, with final rounding inside the bound.

    Bigger readings receive proportionally bigger absolute adjustments. Two
    decimal places are used when possible; tiny high-precision inputs retain
    their original value if no centi-kWh value fits the permitted interval.
    """
    original = Decimal(str(value))
    limit = Decimal(str(percent)) / 100
    if not original.is_finite() or original < 0 or not 0 <= limit <= 1:
        raise ValueError("Consumption must be finite and nonnegative, with noise between 0 and 100%.")
    if original == 0 or limit == 0:
        return float(original)
    message = f"bounded-noise\x1f{column}\x1f{_text(card)}\x1f{_text(period)}\x1f{_text(bill)}".encode("utf-8")
    integer = int.from_bytes(hmac.new(key, message, hashlib.sha256).digest()[:8], "big")
    signed = Decimal(integer) / Decimal(2**63) - 1
    candidate = original * (1 + limit * signed)
    quantum = Decimal("0.01")
    lower = (original * (1 - limit)).quantize(quantum, rounding=ROUND_CEILING)
    upper = (original * (1 + limit)).quantize(quantum, rounding=ROUND_FLOOR)
    if lower > upper:
        return float(original)
    rounded = candidate.quantize(quantum, rounding=ROUND_HALF_UP)
    return float(min(upper, max(lower, rounded)))


def truncate_time(value: object, granularity: str) -> str:
    """Cut a timestamp finer than an hour down to the hour. Coarser values (a month, a date) pass unchanged."""
    text = _text(value)
    match = _FINE_TIMESTAMP.match(text)
    if granularity == "hour" and match:
        return f"{match.group(1)} {match.group(2)}:00"
    return text


def residential_tier(total_kwh: float) -> str:
    if total_kwh <= 200:
        return "residential_0_200"
    return "residential_201_400" if total_kwh <= 400 else "residential_over_400"


def round_half_up(value: float, step: int) -> int:
    return int(math.floor(value / step + 0.5) * step)


def transform(prepared: pd.DataFrame, cookbook: Cookbook, key: bytes) -> tuple[pd.DataFrame, dict]:
    """Return (output table, report). The report says what each rule did, in numbers."""
    params = cookbook.parameters
    minimum = int(params["min_group_size"])
    step = int(params["kwh_round_to"])
    percentile = float(params["kwh_cap_percentile"])
    capped_types = list(params.get("kwh_cap_applies_to", ["resident"]))
    hash_length = int(params.get("hash_hex_chars", 16))
    noise_percent = float(params.get("kwh_noise_percent", 0))
    card_column = cookbook.person_marker["fallback"]
    time_columns = cookbook.columns_with("keep_time")

    produced: dict[str, pd.Series] = {}
    report: dict = {"rows": len(prepared), "deleted_columns": [], "hashed_columns": [], "kwh_caps": {},
                    "kwh_noise_percent": 0.0}
    total_kwh = pd.to_numeric(prepared["kwh_day"]) + pd.to_numeric(prepared["kwh_night"])

    for column, rule in cookbook.rules.items():
        values = prepared[column]

        if rule.action == "delete":
            report["deleted_columns"].append(column)
            continue

        if rule.action == "keep":
            result = values.map(_text)

        elif rule.action == "keep_time":
            result = values.map(lambda v: truncate_time(v, rule.options.get("granularity", "hour")))

        elif rule.action == "keyed_hash":
            result = values.map(lambda v: keyed_hash(key, column, v, hash_length))
            report["hashed_columns"].append(column)

        elif rule.action == "year_only":
            result = values.map(lambda v: year_only(v, int(rule.options.get("band_years", 0))))

        elif rule.action == "clean_location":
            result = prepared[f"_{column}"]              # the agreed spelling from step 2

        elif rule.action == "keep_if_min_group":
            people = prepared.groupby(values.map(_text))["_person_key"].nunique()
            too_small = set(people[people < minimum].index) - {""}
            result = values.map(lambda v: "" if _text(v) in too_small else _text(v))
            report[f"{column}_values_blanked"] = len(too_small)
            report[f"{column}_rows_blanked"] = int(values.map(_text).isin(too_small).sum())

        elif rule.action == "merge_values":
            merge = rule.options.get("merge", {})
            def merged(index, value, merge=merge):
                target = merge.get(_text(value))
                if target is None:
                    return _text(value)
                return residential_tier(total_kwh[index]) if target == "residential_tier_by_kwh" else target
            result = pd.Series([merged(i, v) for i, v in values.items()], index=values.index)
            report[f"{column}_rows_merged"] = int(values.map(_text).isin(merge.keys()).sum())

        elif rule.action == "bounded_noise":
            periods = prepared[time_columns[0]] if time_columns else pd.Series("", index=prepared.index)
            result = pd.Series([
                bounded_kwh_noise(key, column, card, period, bill, value, noise_percent)
                for card, period, bill, value in zip(prepared[card_column], periods, prepared["bill_id"], values)
            ], index=values.index)
            report["kwh_noise_percent"] = noise_percent
            report["kwh_noise_method"] = "Stable per-bill, per-component proportional noise; rounded within bounds"

        elif rule.action == "round_and_cap":
            numbers = pd.to_numeric(values).astype(float)
            capped_rows = prepared["consumer_type"].isin(capped_types)
            cap = float(numbers[capped_rows].quantile(percentile / 100)) if capped_rows.any() else math.inf
            if noise_percent:                            # noise first, then cap, then round
                periods = prepared[time_columns[0]] if time_columns else pd.Series("", index=prepared.index)
                factors = [1 + noise_percent / 100 * unit_noise(key, column, card, period)
                           for card, period in zip(prepared[card_column], periods)]
                numbers = numbers * factors
            over = capped_rows & (numbers > cap)
            numbers = numbers.where(~over, cap)
            result = numbers.map(lambda n: round_half_up(n, step))
            report["kwh_caps"][column] = {"cap": round_half_up(cap, step) if math.isfinite(cap) else None,
                                         "rows_capped": int(over.sum())}
            report["kwh_noise_percent"] = noise_percent

        else:                                            # cookbook.load() already rejects unknown actions
            raise ValueError(f"{column}: no implementation for action {rule.action!r}")

        produced[rule.output] = result

    # The lowest bin: a bill at or below it is published as the bin itself, all of it in the first kWh column,
    # so an empty home (0 kWh) looks like any other very small bill.
    lowest = int(params.get("kwh_lowest_bin", 0))
    kwh_outputs = [cookbook.rules[c].output for c in cookbook.columns_with("round_and_cap")]
    if lowest and kwh_outputs:
        in_bin = sum(produced[c] for c in kwh_outputs) <= lowest
        produced[kwh_outputs[0]] = produced[kwh_outputs[0]].where(~in_bin, lowest)
        for column in kwh_outputs[1:]:
            produced[column] = produced[column].where(~in_bin, 0)
        report["kwh_lowest_bin"] = {"at_or_below": lowest, "rows": int(in_bin.sum())}

    # Exactly the agreed columns, in the agreed order. Working columns and anything unlisted stay behind.
    output = pd.DataFrame({name: produced[name] for name in cookbook.output_columns})
    report["output_columns"] = list(output.columns)
    return output.reset_index(drop=True), report


def leak_scan(source_rows: pd.DataFrame, output: pd.DataFrame, cookbook: Cookbook) -> list[tuple[str, str]]:
    """Look for any raw identifying value that survived into the output.

    Returns (source column, value) pairs. An empty list is the pass condition for step 3.
    Checks every output cell for an exact match, and text cells for the value appearing inside them.
    """
    hashed_outputs = {r.output for r in cookbook.rules.values() if r.action == "keyed_hash"}
    cells, text_cells = set(), set()
    for name in output.columns:
        values = set(output[name].map(_text).unique()) - {""}
        cells |= values
        if name not in hashed_outputs:               # a hash is random hex; digits inside it mean nothing
            text_cells |= {v for v in values if not v.replace(".", "", 1).replace("-", "", 1).isdigit()}

    found: list[tuple[str, str]] = []
    for column in cookbook.leak_scan_columns:
        for value in source_rows[column].map(_text).unique():
            value = value.strip()
            if not value:
                continue
            if value in cells or (len(value) >= 6 and any(value in cell for cell in text_cells)):
                found.append((column, value))
    return found
