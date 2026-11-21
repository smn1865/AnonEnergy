"""Clean location text and mark the person behind each card (roadmap step 2).

Nothing here is written anywhere. It adds working columns to the rows in memory,
so that step 4 can count distinct people per place before step 3 deletes the identifiers.

Working columns added (all start with an underscore and never reach the output):
    _region, _community, _settlement   one agreed spelling per place
    _location_key                       region|community|settlement, safe to group by
    _person_key                         the person or company behind the card
"""
from __future__ import annotations

import re
import unicodedata

import pandas as pd

from .cookbook import Cookbook

_SPACES = re.compile(r"\s+")


def clean_text(value: object) -> str:
    """Trim, collapse inner spaces, and put the text in one Unicode form. Keeps case and spelling."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return _SPACES.sub(" ", unicodedata.normalize("NFC", str(value))).strip()


def strip_prefix(text: str, prefixes: list[str]) -> str:
    """Remove a leading settlement-type prefix such as 'ք.' (city)."""
    lowered = text.casefold()
    for prefix in prefixes:
        if lowered.startswith(prefix.casefold()):
            return text[len(prefix):].strip()
    return text


def match_key(value: object, prefixes: list[str], unknown_label: str) -> str:
    """The form used for comparing: ignores case, spacing, prefixes and the եւ / և difference."""
    text = strip_prefix(clean_text(value), prefixes)
    # NFKC turns the ligature և into ե + ւ, so both spellings of Երևան compare equal.
    key = unicodedata.normalize("NFKC", text).casefold()
    return key or unknown_label


def _agreed_spellings(raw: pd.Series, keys: pd.Series, prefixes: list[str], unknown_label: str) -> dict[str, str]:
    """For each key, pick the spelling most rows already use (ties: alphabetical)."""
    shown = raw.map(lambda v: strip_prefix(clean_text(v), prefixes))
    spellings: dict[str, str] = {}
    for key, group in shown.groupby(keys):
        if key == unknown_label:
            spellings[key] = unknown_label
            continue
        counts = group.value_counts()
        best = counts[counts == counts.max()].index
        spellings[key] = sorted(best)[0]
    return spellings


def prepare(rows: pd.DataFrame, cookbook: Cookbook) -> pd.DataFrame:
    """Return a copy of the rows with the working columns added."""
    out = rows.copy()
    key_columns = []
    for level in cookbook.location_levels:
        keys = out[level].map(lambda v: match_key(v, cookbook.strip_prefixes, cookbook.unknown_label))
        spellings = _agreed_spellings(out[level], keys, cookbook.strip_prefixes, cookbook.unknown_label)
        out[f"_{level}_key"] = keys
        out[f"_{level}"] = keys.map(spellings)
        key_columns.append(f"_{level}_key")

    # One key from all levels together: two villages with the same name stay two places.
    out["_location_key"] = out[key_columns].agg("|".join, axis=1)
    out = out.drop(columns=key_columns)

    out["_person_key"] = out.apply(lambda row: person_key(row, cookbook), axis=1)
    return out


def person_key(row: pd.Series, cookbook: Cookbook) -> str:
    """Identify the person or company behind a card, so one person with three cards counts once."""
    marker = cookbook.person_marker
    column = marker.get(clean_text(row.get("consumer_type")), marker["fallback"])
    value = clean_text(row.get(column))
    if not value:                      # no identity document on file: fall back to the card itself
        column = marker["fallback"]
        value = clean_text(row.get(column))
    return f"{column}:{value}"


def location_summary(prepared: pd.DataFrame, card_column: str = "subscriber_card_no") -> pd.DataFrame:
    """Cards and distinct people per place, smallest first. This is what step 4 will work from."""
    summary = (
        prepared.groupby(["_location_key", "_region", "_community", "_settlement"], sort=False)
        .agg(cards=(card_column, "nunique"), people=("_person_key", "nunique"), rows=(card_column, "size"))
        .reset_index()
        .sort_values(["people", "_location_key"])
        .reset_index(drop=True)
    )
    return summary
