"""Preview policy: the same 14-column release, with bounded decimal kWh noise."""
from dataclasses import replace
from pathlib import Path

from anonymizer import cookbook

NOISE_PERCENT = 1.0


def load_preview_policy():
    base = cookbook.load(Path(__file__).resolve().parent / "cookbook.yaml")
    rules = dict(base.rules)
    for column in ("kwh_day", "kwh_night"):
        rules[column] = replace(rules[column], action="bounded_noise")
    tariff = rules["tariff_category"]
    rules["tariff_category"] = replace(
        tariff, options={**tariff.options,
                         "merge": {**tariff.options.get("merge", {}),
                                   "residential_social": "residential_tier_by_kwh"}},
    )
    # No cap or low-value bin may move a numeric preview reading outside ±1%.
    # Group protection can still suppress a reading or withhold an unsafe account.
    return replace(
        base, version=3, rules=rules,
        parameters={**base.parameters, "kwh_noise_percent": NOISE_PERCENT,
                    "kwh_lowest_bin": 0},
        person_marker={**base.person_marker, "residential": "passport_or_id_no",
                       "commercial": "taxpayer_no"},
        second_database={**base.second_database,
                         "column_types": {**base.second_database.get("column_types", {}),
                                          "kwh_day": "numeric", "kwh_night": "numeric"}},
    )
