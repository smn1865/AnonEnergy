"""Monthly, aggregate energy analytics. No raw identity fields or model-training services.

Functions accept protected rows or aggregate measurements and return tables and metadata.
Missing periods and suppressed readings are never filled with zero.
"""
from __future__ import annotations

import io
import math

import numpy as np
import pandas as pd

CELL = ["region", "consumer_type", "voltage_kv"]
MIN_ACCOUNTS = 5
MODELS = ("Last month", "Trailing 3-month mean", "Seasonal naive (12 months)")


class AnalyticsError(ValueError):
    pass


def _months(values):
    text = values.astype(str)
    if not text.str.fullmatch(r"\d{4}-(0[1-9]|1[0-2])").all():
        raise AnalyticsError("Billing months must use YYYY-MM format.")
    return pd.to_datetime(text + "-01")


def aggregate_consumption(table: pd.DataFrame, minimum: int = MIN_ACCOUNTS) -> tuple[pd.DataFrame, dict]:
    required = ["billing_month", "subscriber_id", *CELL, "kwh_day", "kwh_night"]
    missing = set(required) - set(table.columns)
    if missing:
        raise AnalyticsError("Analysis requires these research columns: " + ", ".join(sorted(missing)))
    if table.empty:
        raise AnalyticsError("There are no research rows to analyze.")
    frame = table[required].copy()
    if frame.subscriber_id.astype(str).str.strip().eq("").any():
        raise AnalyticsError("An account ID is required for coverage and per-account comparisons.")
    frame["month"] = _months(frame.billing_month)
    inconsistent = frame.groupby(["month", "subscriber_id"])[CELL].nunique(dropna=False).gt(1)
    if inconsistent.any().any():
        raise AnalyticsError("An account cannot belong to multiple analysis cohorts in the same month.")
    for col in ["kwh_day", "kwh_night"]:
        original = frame[col]
        frame[col] = pd.to_numeric(original, errors="coerce")
        missing_value = original.isna() | original.astype(str).str.strip().eq("")
        invalid = (~missing_value & frame[col].isna()) | frame[col].lt(0) | np.isinf(frame[col].astype(float))
        if invalid.any():
            raise AnalyticsError(f"{col} contains an invalid or negative measurement.")
    frame["observed"] = frame.kwh_day.notna() & frame.kwh_night.notna()
    frame.loc[~frame.observed, ["kwh_day", "kwh_night"]] = np.nan
    cells = []
    hidden = 0
    for key, group in frame.groupby(["month", *CELL], dropna=False, sort=True):
        observed = group[group.observed]
        # Extra aggregate display rule. Accounts are not described as distinct people.
        accounts = int(group.subscriber_id.nunique())
        observed_accounts = int(observed.subscriber_id.nunique())
        if observed_accounts < minimum:
            hidden += 1
            continue
        day, night = float(observed.kwh_day.sum()), float(observed.kwh_night.sum())
        cells.append({"month": key[0], **dict(zip(CELL, key[1:])), "accounts": accounts,
                      "observed_accounts": observed_accounts, "records": len(group),
                      "observed_records": len(observed), "day_kwh": day, "night_kwh": night,
                      "total_kwh": day + night})
    if not cells:
        raise AnalyticsError(f"No aggregate cell has {minimum} accounts with complete consumption readings.")
    result = _rates(pd.DataFrame(cells))
    return result, {"source_rows": len(table), "missing_reading_rows": int((~frame.observed).sum()),
                    "hidden_aggregate_cells": hidden, "minimum_observed_accounts": minimum,
                    "note": "Five accounts is an additional display threshold, not a new distinct-person anonymity guarantee."}


def _rates(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["days"] = out.month.dt.days_in_month
    out["kwh_per_account"] = out.total_kwh / out.observed_accounts
    out["kwh_per_account_day"] = out.kwh_per_account / out.days
    out["coverage_pct"] = 100 * out.observed_records / out.records
    return out


def monthly_summary(cells: pd.DataFrame) -> pd.DataFrame:
    if cells.empty:
        raise AnalyticsError("No data matches the current filters.")
    out = cells.groupby("month", as_index=False)[
        ["day_kwh", "night_kwh", "total_kwh", "accounts", "observed_accounts", "records", "observed_records"]
    ].sum(min_count=1)
    out = _rates(out).sort_values("month").reset_index(drop=True)
    expected = pd.date_range(out.month.min(), out.month.max(), freq="MS")
    # Calendar reindexing means a missing month cannot become the previous month.
    out = out.set_index("month").reindex(expected).rename_axis("month").reset_index()
    out["mom_total_pct"] = out.total_kwh.pct_change(fill_method=None) * 100
    out["mom_normalized_pct"] = out.kwh_per_account_day.pct_change(fill_method=None) * 100
    out["yoy_normalized_pct"] = (out.kwh_per_account_day / out.kwh_per_account_day.shift(12) - 1) * 100
    return out.replace([np.inf, -np.inf], np.nan)


def seasonal_profile(monthly: pd.DataFrame) -> pd.DataFrame:
    values = monthly[["month", "kwh_per_account_day"]].dropna()
    if len(values) < 24 or monthly.kwh_per_account_day.isna().any():
        return pd.DataFrame(columns=["calendar_month", "daily_kwh_per_account", "years_observed", "index_pct"])
    values = values.assign(calendar_month=values.month.dt.month)
    profile = values.groupby("calendar_month").agg(
        daily_kwh_per_account=("kwh_per_account_day", "mean"), years_observed=("month", "size")
    ).reset_index()
    if len(profile) < 12 or profile.years_observed.min() < 2:
        return profile.iloc[:0]
    mean = profile.daily_kwh_per_account.mean()
    profile["index_pct"] = profile.daily_kwh_per_account / mean * 100 if mean > 0 else np.nan
    return profile


def _predict(values: np.ndarray, model: str, horizon: int) -> np.ndarray:
    history = list(np.asarray(values, dtype=float))
    result = []
    for _ in range(horizon):
        if model == MODELS[0]:
            value = history[-1]
        elif model == MODELS[1]:
            value = float(np.mean(history[-3:]))
        elif model == MODELS[2]:
            if len(history) < 12:
                raise AnalyticsError("A seasonal forecast needs at least 12 training months.")
            value = history[-12]
        else:
            raise AnalyticsError("Unknown forecasting method.")
        result.append(max(0.0, value))
        history.append(result[-1])
    return np.array(result)


def forecast(monthly: pd.DataFrame, column: str = "total_kwh", horizon: int = 3) -> dict:
    if not isinstance(horizon, int) or not 1 <= horizon <= 12:
        raise AnalyticsError("Choose a forecast horizon between 1 and 12 months.")
    source = monthly[["month", column]].copy().sort_values("month")
    if source.empty or source.month.duplicated().any():
        raise AnalyticsError("Forecasts need one observation per month.")
    source[column] = pd.to_numeric(source[column], errors="coerce")
    if source[column].isna().any() or not np.isfinite(source[column]).all() or source[column].lt(0).any():
        raise AnalyticsError("Forecasts need complete nonnegative measurements. Missing months are not assumed to be zero.")
    expected = pd.date_range(source.month.min(), source.month.max(), freq="MS")
    if list(source.month) != list(expected):
        raise AnalyticsError("Forecasts need consecutive months. Fill the missing period with actual data first.")
    values = source[column].to_numpy(dtype=float)
    n = len(values)
    if n < 3:
        raise AnalyticsError("At least three consecutive months are required for a baseline projection.")
    candidates = list(MODELS[:2]) + ([MODELS[2]] if n >= 24 else [])
    # The same dates are held out for all candidates. Each fit sees earlier months only.
    min_train = 12 if MODELS[2] in candidates else 3
    origins = list(range(max(min_train, n - 6), n)) if n >= 6 else []
    scores, validations = [], []
    for model in candidates:
        errors = []
        for origin in origins:
            prediction = _predict(values[:origin], model, 1)[0]
            actual = values[origin]
            error = abs(actual - prediction)
            errors.append(error)
            validations.append({"month": source.month.iloc[origin], "model": model, "actual_kwh": actual,
                                "predicted_kwh": prediction, "absolute_error_kwh": error,
                                "training_through": source.month.iloc[origin - 1]})
        denominator = float(values[origins].sum()) if origins else 0.0
        scores.append({"model": model, "validation_months": len(origins),
                       "mae_kwh": float(np.mean(errors)) if errors else np.nan,
                       "wape_pct": 100 * sum(errors) / denominator if denominator > 0 else np.nan})
    scores = pd.DataFrame(scores)
    validated = len(origins) >= 3
    selected = str(scores.loc[scores.mae_kwh.idxmin(), "model"]) if validated else MODELS[0]
    if not validated:
        horizon = 1
    dates = pd.date_range(source.month.iloc[-1] + pd.offsets.MonthBegin(1), periods=horizon, freq="MS")
    predictions = pd.DataFrame({"month": dates, "forecast_kwh": _predict(values, selected, horizon)})
    return {"selected": selected, "validated": validated, "scores": scores,
            "validation": pd.DataFrame(validations), "forecast": predictions, "history_months": n,
            "note": "Rolling-origin one-month validation; no production accuracy guarantee or statistical confidence interval."}


def _anomaly_flags(series: pd.DataFrame, metric: str, cohort: dict, *, threshold: float = 3.5) -> list[dict]:
    series = series.sort_values("month").set_index("month")
    timeline = pd.date_range(series.index.min(), series.index.max(), freq="MS")
    values = pd.to_numeric(series[metric], errors="coerce").reindex(timeline)
    residuals = values - values.shift(12)
    flags = []
    for i, (month, value) in enumerate(values.items()):
        if pd.isna(value):
            continue
        residual_history = residuals.iloc[max(12, i - 6):i].dropna() if i >= 12 else pd.Series(dtype=float)
        seasonal = i >= 12 and pd.notna(values.iloc[i - 12]) and len(residual_history) >= 3
        if seasonal:
            previous = residual_history.to_numpy()
            current = float(residuals.iloc[i])
            level = float(values.iloc[i - 12])
        else:
            previous = values.iloc[max(0, i - 6):i].dropna().to_numpy()
            if len(previous) < 3:
                continue
            current, level = float(value), 0.0
        median = float(np.median(previous))
        mad = float(np.median(np.abs(previous - median)))
        expected = max(0.0, level + median)
        # Require a material deviation too; tiny readings/noise should not dominate flags.
        floor = max(0.5, abs(expected) * 0.25)
        delta = float(value) - expected
        score = 0.67448975 * (current - median) / mad if mad > 1e-9 else np.nan
        flagged = abs(delta) >= floor and (abs(score) > threshold if np.isfinite(score) else True)
        if flagged:
            flags.append({"month": month, **cohort, "observed": float(value), "expected": expected,
                          "change_pct": delta / expected * 100 if expected > 0 else np.nan,
                          "robust_score": score, "baseline_months": len(previous),
                          "method": "Year-over-year residual baseline" if seasonal else "Trailing median baseline",
                          "finding": "Unusually high" if delta > 0 else "Unusually low"})
    return flags


def consumption_anomalies(cells: pd.DataFrame) -> pd.DataFrame:
    flags = []
    for key, group in cells.groupby(CELL, sort=True):
        flags.extend(_anomaly_flags(group, "kwh_per_account_day", dict(zip(CELL, key))))
    return pd.DataFrame(flags)


def generation_anomalies(telemetry: pd.DataFrame) -> pd.DataFrame:
    flags = []
    if "generation_kwh" not in telemetry:
        return pd.DataFrame()
    for region, group in telemetry.groupby("region"):
        group = group.copy()
        group["daily_generation"] = group.generation_kwh / group.month.dt.days_in_month
        flags.extend(_anomaly_flags(group, "daily_generation", {"region": region}))
    return pd.DataFrame(flags)


def peer_efficiency(cells: pd.DataFrame, ratio_threshold: float = 1.5) -> pd.DataFrame:
    """Leave-one-region-out comparison, matched by month, type and voltage."""
    output = cells[["month", *CELL, "observed_accounts", "kwh_per_account_day"]].copy()
    output["peer_median"] = np.nan
    output["peer_cohorts"] = 0
    for _, group in output.groupby(["month", "consumer_type", "voltage_kv"]):
        for index in group.index:
            peers = group.loc[group.region != output.at[index, "region"], "kwh_per_account_day"].dropna()
            output.at[index, "peer_cohorts"] = len(peers)
            if len(peers) >= 3:
                output.at[index, "peer_median"] = float(peers.median())
    output["peer_ratio"] = output.kwh_per_account_day / output.peer_median.where(output.peer_median > 0)
    output["review"] = output.peer_ratio > ratio_threshold
    return output


def read_telemetry(data: bytes) -> pd.DataFrame:
    if len(data) > 2_000_000:
        raise AnalyticsError("Use an aggregate telemetry file smaller than 2 MB.")
    try:
        frame = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    except (ValueError, UnicodeError) as error:
        raise AnalyticsError("Telemetry must be a UTF-8 CSV.") from error
    required = {"billing_month", "region"}
    allowed = ["generation_kwh", "capacity_kw", "energy_input_kwh", "export_kwh"]
    if not required <= set(frame.columns) or not set(allowed) & set(frame.columns):
        raise AnalyticsError("Telemetry needs billing_month, region, and at least one measurement: generation_kwh, capacity_kw, energy_input_kwh or export_kwh.")
    if frame.empty or len(frame) > 5000:
        raise AnalyticsError("Telemetry needs between 1 and 5,000 monthly regional rows.")
    frame["month"] = _months(frame.billing_month)
    if frame.region.str.strip().eq("").any() or frame.duplicated(["month", "region"]).any():
        raise AnalyticsError("Telemetry requires a named region and one row per region per month. Aggregate meters before uploading.")
    for column in set(allowed) & set(frame.columns):
        original = frame[column]
        frame[column] = pd.to_numeric(original, errors="coerce")
        bad = (original.str.strip().ne("") & frame[column].isna()) | frame[column].lt(0) | np.isinf(frame[column].astype(float))
        if bad.any():
            raise AnalyticsError(f"{column} must contain nonnegative finite values or blanks.")
    return frame[["month", "region", *[c for c in allowed if c in frame]]].sort_values(["month", "region"]).reset_index(drop=True)


def telemetry_monthly(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if frame.empty or column not in frame:
        return pd.DataFrame(columns=["month", column])
    # Only complete regional measurements are added; an unknown component is not zero.
    return frame.groupby("month")[column].agg(lambda s: s.sum() if s.notna().all() else np.nan).reset_index()


def energy_balance(cells: pd.DataFrame, telemetry: pd.DataFrame, boundary_confirmed: bool) -> pd.DataFrame:
    if not boundary_confirmed or telemetry.empty or "energy_input_kwh" not in telemetry or "export_kwh" not in telemetry:
        return pd.DataFrame()
    demand = cells.groupby(["month", "region"], as_index=False)[["total_kwh", "records", "observed_records"]].sum()
    demand.loc[demand.records != demand.observed_records, "total_kwh"] = np.nan
    balance = demand.merge(telemetry[["month", "region", "energy_input_kwh", "export_kwh"]], on=["month", "region"], how="inner")
    balance["unaccounted_kwh"] = balance.energy_input_kwh - balance.total_kwh - balance.export_kwh
    balance["unaccounted_pct"] = 100 * balance.unaccounted_kwh / balance.energy_input_kwh.where(balance.energy_input_kwh > 0)
    return balance


def renewable_comparison(cells: pd.DataFrame, telemetry: pd.DataFrame, boundary_confirmed: bool) -> tuple[pd.DataFrame, dict]:
    if telemetry.empty or "generation_kwh" not in telemetry:
        return pd.DataFrame(), {"correlation": None, "aligned_months": 0}
    generation = telemetry_monthly(telemetry, "generation_kwh")
    demand = monthly_summary(cells)[["month", "total_kwh"]].rename(columns={"total_kwh": "consumption_kwh"})
    paired = demand.merge(generation, on="month", how="inner")
    eligible_months = []
    for month, demand_cells in cells.groupby("month"):
        meters = telemetry.loc[telemetry.month == month]
        if (set(demand_cells.region) == set(meters.region) and
                demand_cells.records.eq(demand_cells.observed_records).all() and
                meters.generation_kwh.notna().all()):
            eligible_months.append(month)
    paired["comparable_regions"] = paired.month.isin(eligible_months)
    paired["generation_to_consumption_pct"] = np.nan
    correlation = None
    usable = paired.loc[paired.comparable_regions, ["consumption_kwh", "generation_kwh"]].dropna()
    if boundary_confirmed:
        comparable = paired.comparable_regions & paired.consumption_kwh.gt(0)
        paired.loc[comparable, "generation_to_consumption_pct"] = 100 * paired.loc[comparable, "generation_kwh"] / paired.loc[comparable, "consumption_kwh"]
        if len(usable) >= 6 and usable.nunique().min() > 1:
            correlation = float(usable.corr().iloc[0, 1])
    return paired, {"correlation": correlation, "aligned_months": len(usable)}


def capacity_factors(telemetry: pd.DataFrame) -> pd.DataFrame:
    if not {"generation_kwh", "capacity_kw"} <= set(telemetry.columns):
        return pd.DataFrame()
    out = telemetry[["month", "region", "generation_kwh", "capacity_kw"]].copy()
    out["capacity_factor_pct"] = 100 * out.generation_kwh / (out.capacity_kw.where(out.capacity_kw > 0) * out.month.dt.days_in_month * 24)
    out["check_metering"] = out.capacity_factor_pct > 100
    return out


def synthetic_reference() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Public aggregate example, unrelated to the workspace's source/encrypted package.

    24 months, four fictional regional cohorts, two types, 20 accounts in every cell.
    Generation and input-energy figures are simulated measurements, never inferred from bills.
    """
    rows, meter_rows = [], []
    months = pd.date_range("2024-09-01", periods=24, freq="MS")
    regions = ["Երևան", "Շիրակ", "Սյունիք", "Կոտայք"]
    for i, month in enumerate(months):
        for r, region in enumerate(regions):
            total = 0.0
            for consumer_type, base in [("resident", 7.5), ("business", 28.0)]:
                winter = 1 + 0.27 * math.cos(2 * math.pi * (month.month - 1) / 12)
                growth = 1 + i * 0.003
                rate = base * winter * growth * (1 + r * 0.045) * (1 + .018 * math.sin(i * 1.7 + r))
                if r == 3 and consumer_type == "resident" and i >= 16:
                    rate *= 1.65
                if r == 1 and consumer_type == "business" and i == 23:
                    rate *= 2.2
                amount = round(rate * month.days_in_month * 20, 2)
                day = round(amount * (0.68 + 0.025 * math.sin(i)), 2)
                rows.append({"month": month, "region": region, "consumer_type": consumer_type,
                             "voltage_kv": "0.38", "accounts": 20, "observed_accounts": 20,
                             "records": 20, "observed_records": 20, "day_kwh": day,
                             "night_kwh": amount - day, "total_kwh": amount})
                total += amount
            capacity = 64 + r * 8
            factor = .17 + .075 * math.sin(2 * math.pi * (month.month - 3) / 12)
            generation = capacity * 24 * month.days_in_month * factor * (1 + .015 * math.sin(i + r))
            if r == 2 and i == 22:
                generation *= .25
            export = generation * .08
            unaccounted_fraction = .06 + .01 * r
            meter_rows.append({"month": month, "region": region, "generation_kwh": round(generation, 2),
                               "capacity_kw": capacity, "energy_input_kwh": round((total + export) / (1 - unaccounted_fraction), 2),
                               "export_kwh": round(export, 2)})
    return _rates(pd.DataFrame(rows)), pd.DataFrame(meter_rows)
