"""Streamlit presentation for the five aggregate analytics modules."""
from __future__ import annotations

import calendar
import html

import altair as alt
import pandas as pd
import streamlit as st

import energy_analytics as ea

GREEN = "#368000"
DARK = "#23471c"
YELLOW = "#bf9600"
MODULES = ["Energy consumption analysis", "Energy forecasting", "Anomaly detection",
           "Energy efficiency analysis", "Renewable energy analysis"]


@st.cache_data(show_spinner=False)
def _reference():
    return ea.synthetic_reference()


def _line(frame, value, label, *, color=GREEN, height=285, key=None):
    chart = alt.Chart(frame).mark_line(point=True, color=color).encode(
        x=alt.X("month:T", title="Billing month", axis=alt.Axis(format="%b %Y", labelAngle=-35)),
        y=alt.Y(f"{value}:Q", title=label, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("month:T", title="Month", format="%Y-%m"), alt.Tooltip(f"{value}:Q", title=label, format=",.2f")],
    ).properties(height=height)
    st.altair_chart(chart, width="stretch", key=key)


def _table(frame, key):
    st.dataframe(frame, hide_index=True, width="stretch", key=key)


def _consumption(cells, monthly):
    st.subheader("Consumption patterns and comparisons")
    metric = st.selectbox("Consumption measure", ["Total kWh", "kWh per observed account per day"], key="analytics_consumption_measure")
    column = "total_kwh" if metric == "Total kWh" else "kwh_per_account_day"
    _line(monthly, column, metric)
    valid = monthly.dropna(subset=["total_kwh"])
    latest = valid.iloc[-1]
    peak = valid.loc[valid.total_kwh.idxmax()]
    a, b, c = st.columns(3)
    a.metric("Highest observed billing month", peak.month.strftime("%b %Y"))
    b.metric("Latest total change", f"{latest.mom_total_pct:+.1f}%" if pd.notna(latest.mom_total_pct) else "Not available")
    c.metric("Latest normalized change", f"{latest.mom_normalized_pct:+.1f}%" if pd.notna(latest.mom_normalized_pct) else "Not available")
    st.caption("Normalized change adjusts for the number of observed accounts and days in the month. Different cohort composition can still affect comparisons.")
    a, b = st.columns(2)
    with a:
        st.markdown("**Day / night billed energy**")
        shares = pd.DataFrame({"Period": ["Day", "Night"], "kWh": [valid.day_kwh.sum(), valid.night_kwh.sum()]})
        st.altair_chart(alt.Chart(shares).mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5).encode(
            x=alt.X("Period:N", title=None), y=alt.Y("kWh:Q"), color=alt.Color("Period:N", scale=alt.Scale(domain=["Day", "Night"], range=[GREEN, "#d8ac12"]), legend=None), tooltip=["Period", alt.Tooltip("kWh:Q", format=",.0f")]
        ).properties(height=230), width="stretch")
    with b:
        st.markdown("**Latest month by region**")
        by_region = cells[cells.month == latest.month].groupby("region", as_index=False).total_kwh.sum()
        st.altair_chart(alt.Chart(by_region).mark_bar(color=GREEN, cornerRadiusEnd=4).encode(
            y=alt.Y("region:N", title=None, sort="-x"), x=alt.X("total_kwh:Q", title="kWh"), tooltip=["region", alt.Tooltip("total_kwh:Q", format=",.0f")]
        ).properties(height=230), width="stretch")
    profile = ea.seasonal_profile(monthly)
    st.markdown("**Seasonal variation**")
    if profile.empty:
        st.info(f"This selection has {len(valid)} observed months. Two complete yearly cycles (24 consecutive months) are required here before displaying a seasonal profile. Use the synthetic reference to explore it.")
    else:
        profile = profile.assign(calendar_label=profile.calendar_month.map(lambda m: calendar.month_abbr[m]))
        st.altair_chart(alt.Chart(profile).mark_bar(color=GREEN).encode(
            x=alt.X("calendar_label:N", title="Calendar month", sort=list(calendar.month_abbr)[1:]),
            y=alt.Y("index_pct:Q", title="Seasonal index (average = 100)"),
            tooltip=["calendar_label", alt.Tooltip("index_pct:Q", format=".1f"), "years_observed"]
        ).properties(height=220), width="stretch")
        yoy = monthly[["month", "yoy_normalized_pct"]].dropna().tail(6)
        st.markdown("**Same-month comparison with the previous year**")
        _table(yoy.rename(columns={"yoy_normalized_pct": "Normalized change vs previous year (%)"}), "analytics_yoy")
    st.warning("Monthly bills identify the highest-energy billing month, not an hourly peak-demand period or peak kW. Intraday peaks need timestamped interval energy or power readings.")
    with st.expander("Calculation logic: consumption"):
        st.code("total_kwh = sum(complete day_kwh + night_kwh)\nnormalized_rate = total_kwh / observed_accounts / calendar_days\nMoM = (current / previous_calendar_month - 1) * 100\nseasonal_index = calendar_month_mean(normalized_rate) / overall_month_mean * 100")
        st.write("Only complete day/night pairs enter totals. A suppressed reading is unknown, not zero. Small aggregate cells with fewer than five observed accounts are excluded. The per-account threshold supplements the original protection; account counts are not distinct-person counts.")


def _forecast(cells, monthly, telemetry):
    st.subheader("Forecast future monthly energy")
    targets = ["Consumption"]
    if not telemetry.empty and "generation_kwh" in telemetry and telemetry.generation_kwh.notna().any():
        targets.append("Generation")
    target = st.selectbox("Forecast target", targets, key="analytics_forecast_target")
    history = monthly[["month", "total_kwh"]] if target == "Consumption" else ea.telemetry_monthly(telemetry, "generation_kwh").rename(columns={"generation_kwh": "total_kwh"})
    long_history = len(history) >= 6
    horizon = st.select_slider("Months ahead", options=list(range(1, 7)) if long_history else [1], value=3 if long_history else 1, key="analytics_horizon")
    try:
        result = ea.forecast(history, horizon=horizon)
    except ea.AnalyticsError as error:
        st.info(str(error))
        return
    if result["validated"]:
        st.success(f"Selected baseline: {result['selected']}. Compared using the same rolling one-month holdouts, with no future data in training.")
    else:
        st.warning("Short-history baseline projection only. Fewer than six months cannot support the validation used here; no accuracy score is claimed and the horizon is limited to one month.")
    forecast = result["forecast"]
    continuation = pd.concat([pd.DataFrame({"month": [history.month.iloc[-1]], "forecast_kwh": [history.total_kwh.iloc[-1]]}), forecast])
    actual_chart = alt.Chart(history).mark_line(point=True, color=DARK).encode(
        x=alt.X("month:T", title="Month", axis=alt.Axis(format="%b %Y", labelAngle=-35)),
        y=alt.Y("total_kwh:Q", title="Monthly kWh", scale=alt.Scale(zero=False)), tooltip=[alt.Tooltip("month:T", format="%Y-%m"), alt.Tooltip("total_kwh:Q", format=",.0f")])
    forecast_chart = alt.Chart(continuation).mark_line(point=True, color=YELLOW, strokeDash=[6, 4]).encode(
        x="month:T", y="forecast_kwh:Q", tooltip=[alt.Tooltip("month:T", format="%Y-%m"), alt.Tooltip("forecast_kwh:Q", format=",.0f")])
    st.altair_chart((actual_chart + forecast_chart).properties(height=310), width="stretch")
    st.caption("Green: observed history. Gold dashed: forecast. These are transparent statistical baselines, not a trained weather or grid-dispatch model.")
    _table(forecast, "analytics_forecast_values")
    st.markdown("**Model comparison**")
    _table(result["scores"], "analytics_model_scores")
    if result["validated"]:
        selected = result["validation"][result["validation"].model == result["selected"]]
        with st.expander("Actual vs predicted values in the held-out months"):
            _table(selected, "analytics_validation")
    with st.expander("Calculation logic: forecasting"):
        st.write("Last month repeats the latest observation. The trailing mean averages the latest three values, appending each prediction for a multi-month forecast. Seasonal naive repeats the value from the same month a year earlier and is offered only with 24 months of history.")
        st.code("for held_out_month in last_up_to_6_months:\n    prediction = model(history_before_held_out_month)\n    error = abs(actual - prediction)\nMAE = mean(error)\nWAPE = sum(error) / sum(actual) * 100\nselected_model = model_with_lowest_MAE")
        st.write("All candidates use the same holdout dates. Zero actual totals leave WAPE undefined. Gaps stop forecasting rather than being filled with zero. One-month validation does not establish six-month accuracy. No confidence interval is invented.")


def _anomalies(cells, telemetry):
    st.subheader("Find unusual aggregate patterns")
    st.caption("Review candidates, not proof of fraud, faults or meter errors. Baselines use only earlier observations.")
    flags = ea.consumption_anomalies(cells)
    recent_start = cells.month.max() - pd.DateOffset(months=5)
    recent = flags[flags.month >= recent_start] if not flags.empty else flags
    a, b = st.columns(2)
    a.metric("Consumption review flags · last 6 months", len(recent))
    b.metric("Months in this selection", cells.month.nunique())
    if cells.month.nunique() < 4:
        st.info("At least three earlier observations are needed to assess a new month. This selection is too short for temporal anomaly detection; a zero count here is not evidence that no anomalies exist.")
    elif recent.empty:
        st.info("No recent consumption aggregates crossed both configured review thresholds.")
    else:
        _table(recent.sort_values("month", ascending=False).rename(columns={"observed": "Observed kWh/account/day", "expected": "Expected kWh/account/day"}), "analytics_anomaly_consumption")
    if not telemetry.empty and "generation_kwh" in telemetry:
        st.markdown("**Production review flags**")
        production = ea.generation_anomalies(telemetry)
        production = production[production.month >= recent_start] if not production.empty else production
        if production.empty:
            st.caption("No recent production flags, or insufficient prior production measurements.")
        else:
            _table(production.sort_values("month", ascending=False).rename(columns={"observed": "Observed generation kWh/day", "expected": "Expected generation kWh/day"}), "analytics_anomaly_generation")
    else:
        st.caption("Production anomalies become available when monthly generation measurements are supplied. The solar flag in the source bills is not a generation measurement.")
    with st.expander("Calculation logic: anomaly detection"):
        st.code("x = monthly kWh / observed accounts / calendar days\nmedian = median(previous_up_to_6_observations)\nMAD = median(abs(history - median))\nrobust_score = 0.67448975 * (x - median) / MAD\nflag = abs(robust_score) > 3.5 AND material_change >= max(0.5, expected * 25%)")
        st.write("Once enough year-over-year residuals exist, the same detector operates on x(t) − x(t−12), reducing seasonal false alarms. Before that it uses a trailing level baseline, which can flag normal seasonal changes. If MAD is zero, the material-change rule is used instead of dividing by zero. Production uses kWh/day rather than per-account values. A flag calls for investigation and is not a formal outlier hypothesis test.")


def _efficiency(full_cells, cells, telemetry, boundary_ok):
    st.subheader("Efficiency screening and energy balance")
    st.write("Compare normalized consumption with similar cohorts. High usage is a review signal; bills alone do not establish energy waste.")
    peers = ea.peer_efficiency(full_cells)
    visible = cells[["month", *ea.CELL]].drop_duplicates()
    peers = peers.merge(visible, on=["month", *ea.CELL], how="inner")
    latest = peers[peers.month == cells.month.max()]
    a, b = st.columns(2)
    a.metric("Latest cohorts above 1.5× peer median", int(latest.review.sum()))
    b.metric("Cohorts with a usable peer benchmark", int(latest.peer_median.notna().sum()))
    _table(latest.rename(columns={"kwh_per_account_day": "kWh/account/day", "peer_median": "Other-region peer median", "peer_ratio": "Usage / peer median", "review": "Needs review"}), "analytics_efficiency_peers")
    st.caption("Peers match billing month, consumer type and voltage. The tested region is excluded from its benchmark; at least three other regional cohorts are required. Benchmarks use the full dataset, so selecting one region does not remove its peers.")
    st.markdown("**Unaccounted energy**")
    balance = ea.energy_balance(cells, telemetry, boundary_ok)
    if balance.empty:
        st.info("Actual losses cannot be inferred from customer bills. To screen the energy balance, supply energy_input_kwh and export_kwh for the same metering boundary and periods, retain all consumer types, and confirm matching coverage.")
    else:
        latest_balance = balance[balance.month == balance.month.max()]
        _table(latest_balance, "analytics_energy_balance")
        if balance.unaccounted_kwh.lt(0).any():
            st.warning("Some balances are negative. Check period alignment, measurement coverage, billing adjustments and the boundary definition.")
        st.caption("Unaccounted energy = boundary input − billed consumption − boundary exports. This can contain technical losses, timing differences, billing errors or other unmetered flows; it is not a measured technical-loss rate.")
    with st.expander("Calculation logic: efficiency"):
        st.code("rate = billed kWh / observed accounts / days\npeer_median = median(other_regions_with_same_month_type_voltage)\nreview = rate / peer_median > 1.5\nunaccounted_kwh = energy_input_kwh - billed_kwh - export_kwh")
        st.write("This is cohort screening, not a building efficiency score: floor area, weather, occupancy and equipment are absent. Suppressed or missing billed readings disable their regional energy-balance calculation.")


def _renewables(cells, telemetry, boundary_ok):
    st.subheader("Renewable production and demand")
    if telemetry.empty or "generation_kwh" not in telemetry or not telemetry.generation_kwh.notna().any():
        st.info("The billing sample contains a has_solar flag but no generated-energy measurement, and the flag is removed from the protected table. Upload monthly generation data or select the 24-month synthetic reference to run renewable analysis.")
        st.code("Required: billing_month, region, generation_kwh\nOptional: capacity_kw\nFor energy-balance screening: energy_input_kwh, export_kwh")
        return
    generation = ea.telemetry_monthly(telemetry, "generation_kwh")
    _line(generation, "generation_kwh", "Generation (kWh)")
    paired, comparison = ea.renewable_comparison(cells, telemetry, boundary_ok)
    a, b, c = st.columns(3)
    a.metric("Measured generation in selection", f"{generation.generation_kwh.sum(min_count=1):,.0f} kWh")
    b.metric("Aligned complete months", comparison["aligned_months"])
    c.metric("Production / consumption correlation", f"{comparison['correlation']:.2f}" if comparison["correlation"] is not None else "Not available")
    if not boundary_ok:
        st.info("Generation trends can be shown on their own. Confirm matching boundaries and all consumer types before calculating ratios or correlations with demand.")
    elif not paired.empty:
        comparable = paired[paired.comparable_regions]
        if len(comparable) < len(paired):
            st.warning("Some months do not contain the same regions or complete demand readings. Their comparison ratios are withheld.")
        _table(paired, "analytics_renewable_comparison")
    factors = ea.capacity_factors(telemetry)
    if not factors.empty:
        st.markdown("**Monthly capacity utilization**")
        _table(factors[factors.month == factors.month.max()], "analytics_capacity_factor")
        if factors.check_metering.any():
            st.warning("A capacity factor exceeds 100%. Check capacity units, generation units and the aggregation boundary.")
    st.caption("Generation/consumption can exceed 100%; it is not a self-consumption rate. Correlation requires at least six complete aligned months and nonconstant values, and does not establish causation. Monthly totals cannot reveal within-day solar-demand matching.")
    with st.expander("Calculation logic: renewable energy"):
        st.code("generation_to_consumption_pct = measured_generation / billed_consumption * 100\ncapacity_factor_pct = generation_kWh / (installed_capacity_kW * 24 * calendar_days) * 100\nrelationship = PearsonCorrelation(aligned_monthly_generation, aligned_monthly_consumption)")
        st.write("Only actual uploaded measurements or the explicitly synthetic reference supply generation; the solar flag is never converted into invented kWh. A zero/unknown capacity leaves the capacity factor undefined. Ratios and correlation require confirmed, complete and matching coverage.")


def _card_title(title, tag="Available", pending=False):
    css = "analysis-tag pending" if pending else "analysis-tag"
    st.markdown(f'<div class="dashboard-card-label"><h3>{html.escape(title)}</h3>'
                f'<span class="{css}">{html.escape(tag)}</span></div>', unsafe_allow_html=True)


def _number(value, description):
    st.markdown(f'<div class="dashboard-number">{html.escape(value)}</div>'
                f'<p class="dashboard-description">{html.escape(description)}</p>', unsafe_allow_html=True)


def _empty_card(title, description):
    st.markdown(f'<div class="small-empty"><strong>{html.escape(title)}</strong>'
                f'<p>{html.escape(description)}</p></div>', unsafe_allow_html=True)


def _overview(full_cells, cells, monthly, telemetry, boundary_ok):
    valid = monthly.dropna(subset=["total_kwh"])
    latest = valid.iloc[-1]
    has_generation = not telemetry.empty and "generation_kwh" in telemetry and telemetry.generation_kwh.notna().any()
    left, right = st.columns([1.2, 1])
    with left, st.container(border=True):
        _card_title("Energy consumption analysis", "Patterns & seasonality")
        _number(f"{valid.total_kwh.sum():,.0f} kWh", "Billed energy across the selected period")
        _line(monthly, "total_kwh", "Monthly consumption (kWh)", height=210, key="overview_consumption")
        peak = valid.loc[valid.total_kwh.idxmax()]
        total = valid.total_kwh.sum()
        day_share = valid.day_kwh.sum() / total * 100 if total > 0 else 0
        st.caption(f"Highest billing month: {peak.month.strftime('%b %Y')} · Day share: {day_share:.1f}%")
        profile = ea.seasonal_profile(monthly)
        st.caption("Two yearly cycles available for seasonal comparisons." if not profile.empty
                   else "Annual seasonality needs 24 months. Hourly peak demand needs interval readings.")
    with right, st.container(border=True):
        _card_title("Energy forecasting", "Demand outlook")
        try:
            prediction = ea.forecast(monthly, horizon=3)
            future = prediction["forecast"]
            _number(f"{future.forecast_kwh.iloc[0]:,.0f} kWh", f"Baseline projection · {future.month.iloc[0].strftime('%B %Y')}")
            actual = alt.Chart(monthly).mark_line(color=DARK).encode(
                x=alt.X("month:T", title=None, axis=alt.Axis(format="%b %y", labelAngle=-35)),
                y=alt.Y("total_kwh:Q", title="kWh", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("month:T", format="%Y-%m"), alt.Tooltip("total_kwh:Q", format=",.0f")])
            continuation = pd.concat([pd.DataFrame({"month": [latest.month], "forecast_kwh": [latest.total_kwh]}), future])
            outlook = alt.Chart(continuation).mark_line(color=YELLOW, strokeDash=[5, 4], point=True).encode(
                x="month:T", y="forecast_kwh:Q",
                tooltip=[alt.Tooltip("month:T", format="%Y-%m"), alt.Tooltip("forecast_kwh:Q", format=",.0f")])
            st.altair_chart((actual + outlook).properties(height=210), width="stretch", key="overview_forecast")
            selected = prediction["scores"].loc[prediction["scores"].model == prediction["selected"]].iloc[0]
            st.caption(f"{prediction['selected']} · Green: observed · Gold: forecast")
            st.caption(f"One-month holdout MAE: {selected.mae_kwh:,.0f} kWh. Longer-horizon accuracy is unvalidated."
                       if prediction["validated"] else "Short-history projection. No validated accuracy score.")
        except ea.AnalyticsError as error:
            _empty_card("More history needed", str(error))
    left, right = st.columns([1.2, 1])
    with left, st.container(border=True):
        _card_title("Anomaly detection", "Consumption & production")
        flags = ea.consumption_anomalies(cells)
        recent_start = cells.month.max() - pd.DateOffset(months=5)
        recent = flags[flags.month >= recent_start] if not flags.empty else flags
        _number(str(len(recent)), "Consumption review flags in the last six months")
        if cells.month.nunique() < 4:
            _empty_card("More history needed", "At least three prior monthly observations are needed before assessing a new month.")
        elif recent.empty:
            _empty_card("No recent review flags", "Available consumption aggregates stayed within the configured thresholds. Review flags are not proof of faults or fraud.")
        else:
            assessed_months = monthly.loc[monthly.month >= recent_start, "month"]
            counts = recent.groupby("month").size().reindex(assessed_months, fill_value=0).rename_axis("month").reset_index(name="flags")
            counts["period"] = counts.month.dt.strftime("%b %Y")
            chart = alt.Chart(counts).mark_bar(color="#e0b420", cornerRadiusTopLeft=5, cornerRadiusTopRight=5).encode(
                x=alt.X("period:O", title=None, sort=counts.period.tolist(), axis=alt.Axis(labelAngle=-25)),
                y=alt.Y("flags:Q", title="Review flags", axis=alt.Axis(tickMinStep=1)),
                tooltip=[alt.Tooltip("month:T", format="%Y-%m"), "flags:Q"]).properties(height=200)
            st.altair_chart(chart, width="stretch", key="overview_anomaly")
            st.caption("Compared with prior cohort history; seasonal residuals are used when enough history exists.")
        if has_generation:
            production = ea.generation_anomalies(telemetry)
            production = production[production.month >= recent_start] if not production.empty else production
            st.caption(f"Production review flags in the same period: {len(production)}")
        else:
            st.caption("Production anomalies require generation measurements.")
    with right, st.container(border=True):
        _card_title("Energy efficiency analysis", "Peer benchmarks")
        peers = ea.peer_efficiency(full_cells).merge(cells[["month", *ea.CELL]].drop_duplicates(), on=["month", *ea.CELL])
        peers = peers[peers.month == latest.month]
        usable = peers.dropna(subset=["peer_ratio"])
        _number(str(int(peers.review.sum())), "Latest cohorts above 1.5× their matched peer median")
        if usable.empty:
            _empty_card("Peer coverage needed", "A benchmark requires at least three other regions with the same month, consumer type and voltage.")
        else:
            usable = usable.assign(benchmark=usable.region.astype(str) + " · " + usable.consumer_type.astype(str)
                                   + " · " + usable.voltage_kv.astype(str) + " kV")
            chart = alt.Chart(usable).mark_bar(cornerRadiusEnd=4).encode(
                y=alt.Y("benchmark:N", title=None, sort="-x", axis=alt.Axis(labelLimit=230)),
                x=alt.X("peer_ratio:Q", title="Consumption / peer median", stack=None),
                color=alt.Color("review:N", scale=alt.Scale(domain=[False, True], range=[GREEN, "#d5ac20"]), legend=None),
                tooltip=["region", "consumer_type", "voltage_kv", alt.Tooltip("peer_ratio:Q", format=".2f")])
            threshold = alt.Chart(pd.DataFrame({"threshold": [1.5]})).mark_rule(color="#a58a30", strokeDash=[4, 4]).encode(x="threshold:Q")
            st.altair_chart((chart + threshold).properties(height=200), width="stretch", key="overview_efficiency")
            st.caption("Adjusted for observed accounts and month length. Higher use is a review signal, not proof of waste.")
        balance = ea.energy_balance(cells, telemetry, boundary_ok)
        if not balance.empty:
            latest_balance = balance[balance.month == balance.month.max()].dropna(subset=["unaccounted_kwh", "energy_input_kwh"])
            incoming = latest_balance.energy_input_kwh.sum()
            st.caption(f"Unaccounted energy: {100 * latest_balance.unaccounted_kwh.sum() / incoming:.1f}% of matched input. This is not a technical-loss estimate."
                       if incoming > 0 else "Energy balance has no positive measured input.")
        else:
            st.caption("Energy-loss screening needs matched input and export measurements.")
    with st.container(border=True):
        _card_title("Renewable energy analysis", "Generation & demand" if has_generation else "Measurements needed", not has_generation)
        if not has_generation:
            _empty_card("Add generation measurements to complete the picture.",
                        "Monthly generation enables production trends and demand comparisons. Installed capacity adds capacity utilization. The 24-month synthetic reference illustrates these views.")
        else:
            chart_column, detail_column = st.columns([2.5, 1])
            generation = ea.telemetry_monthly(telemetry, "generation_kwh")
            paired, info = ea.renewable_comparison(cells, telemetry, boundary_ok)
            with chart_column:
                _line(generation, "generation_kwh", "Renewable generation (kWh)", height=230, key="overview_renewable")
            with detail_column:
                _number(f"{generation.generation_kwh.sum(min_count=1):,.0f} kWh", "Generation across the selected period")
                _number(f"{info['correlation']:.2f}" if info["correlation"] is not None else "—", "Monthly production / consumption correlation")
                factors = ea.capacity_factors(telemetry)
                st.caption("Capacity utilization is available in Renewables." if not factors.empty else "Installed capacity is needed for capacity utilization.")
                st.caption("Comparisons require matching boundaries. Correlation does not establish causation.")


def render_analytics(demo):
    filters = st.columns([1.5, 1, 1])
    with filters[0]:
        dataset = st.selectbox("Analysis dataset", ["Prepared research dataset", "24-month synthetic reference"], key="analytics_dataset")
    telemetry = pd.DataFrame()
    reference = dataset == "24-month synthetic reference"
    if reference:
        cells, telemetry = _reference()
        st.caption("SYNTHETIC REFERENCE · Sep 2024–Aug 2026 · Simulated consumption, generation and boundary measurements. Separate from the customer dataset.")
        meta = {"missing_reading_rows": 0, "hidden_aggregate_cells": 0}
    else:
        if demo.protected is None:
            st.info("Prepare the customer dataset in Data workspace to open its dashboard, or choose the synthetic reference above.")
            left, right = st.columns(2)
            for index, (title, description) in enumerate([
                (MODULES[0], "Patterns, highest-consumption months and seasonal variation."),
                (MODULES[1], "Baseline demand and generation forecasts, with historical model comparisons."),
                (MODULES[2], "Unusual consumption and production patterns."),
                (MODULES[3], "Matched-peer comparisons and measured energy-balance screening."),
                (MODULES[4], "Generation patterns, capacity utilization and relationships with demand."),
            ]):
                with left if index % 2 == 0 else right:
                    with st.container(border=True):
                        _card_title(title, "Prepare data", True)
                        st.caption(description)
            return
        try:
            cells, meta = ea.aggregate_consumption(demo.protected)
        except ea.AnalyticsError as error:
            st.error(str(error))
            return
        with st.expander("Additional generation and boundary measurements"):
            st.caption("Optional monthly regional measurements. Match the research region labels and periods; do not include customer identifiers.")
            upload = st.file_uploader("Monthly regional measurements CSV", type=["csv"], key="analytics_telemetry")
            st.download_button("Download measurement template", "billing_month,region,generation_kwh,capacity_kw,energy_input_kwh,export_kwh\n",
                               "monthly-measurements-template.csv", mime="text/csv", key="analytics_template")
            if upload:
                try:
                    telemetry = ea.read_telemetry(upload.getvalue())
                except ea.AnalyticsError as error:
                    st.error(str(error))
            st.caption("Generation is measured renewable output. Input includes all entering energy, including local generation; exports are energy leaving the boundary.")
    full_cells = cells.copy()
    with filters[1]:
        region = st.selectbox("Region", ["All regions", *sorted(cells.region.unique())], key="analytics_region")
    with filters[2]:
        consumer = st.selectbox("Consumer type", ["All types", *sorted(cells.consumer_type.unique())], key="analytics_type")
    if region != "All regions":
        cells = cells[cells.region == region]
        if not telemetry.empty:
            telemetry = telemetry[telemetry.region == region]
    if consumer != "All types":
        cells = cells[cells.consumer_type == consumer]
    if cells.empty:
        st.info("No eligible aggregates match these filters.")
        return
    if reference:
        boundary_ok = consumer == "All types"
    elif telemetry.empty:
        boundary_ok = False
    else:
        boundary_ok = st.checkbox("Measurements cover the same accounts, metering boundary and billing periods",
                                  value=False, key="analytics_boundary") and consumer == "All types"
    if consumer != "All types" and not telemetry.empty:
        st.caption("Regional generation covers all consumer types. Demand ratios and energy-balance comparisons are disabled for this narrower selection.")
    if meta["hidden_aggregate_cells"]:
        boundary_ok = False
        st.caption("Some small cells are withheld. Whole-boundary comparisons are disabled for this incomplete demand selection.")
    monthly = ea.monthly_summary(cells)
    observed = monthly.dropna(subset=["total_kwh"])
    latest = observed.iloc[-1]
    a, b, c, d = st.columns(4)
    a.metric("Observed months", len(observed))
    b.metric("Latest consumption", f"{latest.total_kwh:,.0f} kWh")
    c.metric("Observed accounts · latest", f"{int(latest.observed_accounts):,}")
    d.metric("Reading coverage · latest", f"{latest.coverage_pct:.1f}%")
    if meta["missing_reading_rows"] or meta["hidden_aggregate_cells"]:
        st.caption(f"{meta['missing_reading_rows']} incomplete/suppressed reading rows · {meta['hidden_aggregate_cells']} small aggregate cells withheld. Totals describe available observations.")
    sections = st.tabs(["Overview", "Consumption", "Forecasting", "Anomalies", "Efficiency", "Renewables"],
                       key="analytics_view", on_change="rerun")
    with sections[0]:
        _overview(full_cells, cells, monthly, telemetry, boundary_ok)
    with sections[1]:
        _consumption(cells, monthly)
    with sections[2]:
        _forecast(cells, monthly, telemetry)
    with sections[3]:
        _anomalies(cells, telemetry)
    with sections[4]:
        _efficiency(full_cells, cells, telemetry, boundary_ok)
    with sections[5]:
        _renewables(cells, telemetry, boundary_ok)
    with st.expander("Data coverage and methodology"):
        st.caption("Calculated locally on protected research rows or the labelled synthetic reference. Monthly bills do not establish hourly peak power, self-consumption or technical losses. No live database connection or computation on encrypted ciphertext is represented.")
        st.caption("Cells require five observed accounts; accounts are not distinct-person counts. Missing readings stay unknown. These aggregates do not establish a new anonymity or differential-privacy guarantee.")
        st.download_button("Download monthly summary", monthly.to_csv(index=False).encode("utf-8-sig"), "monthly-energy-summary.csv",
                           mime="text/csv", key="analytics_summary_download")
        _table(monthly[["month", "total_kwh", "observed_accounts", "coverage_pct"]], "analytics_coverage")
