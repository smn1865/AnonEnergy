"""Anon Energy's local data workspace and energy dashboard."""
from __future__ import annotations

import html
import io

import pandas as pd
import streamlit as st

from analytics_ui import render_analytics
from demo_service import ROOT, SAMPLE, DemoError, DemoSession

st.set_page_config(page_title="Anon Energy · Workspace", page_icon="🌱", layout="wide",
                   initial_sidebar_state="expanded")
st.markdown(f"<style>{(ROOT / 'assets' / 'workspace.css').read_text()}</style>", unsafe_allow_html=True)

if "demo" not in st.session_state:
    st.session_state.demo = DemoSession()
demo: DemoSession = st.session_state.demo


def navigate(page):
    st.session_state.workspace_page = page


def reset_dataset():
    st.session_state.demo = DemoSession()
    st.session_state.workspace_page = "Data workspace"
    st.session_state.dataset_view = "Original dataset"
    for key in ("source_search", "protected_search", "analytics_region", "analytics_type"):
        st.session_state.pop(key, None)


@st.cache_data(show_spinner=False)
def source_table(content):
    return pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def show_table(table, *, key, noun="records"):
    query = st.text_input("Search dataset", placeholder="Search across every column…", key=key + "_search",
                          label_visibility="collapsed", icon=":material/search:")
    shown = table
    if query.strip():
        needle = query.strip().casefold()
        matches = table.astype(str).apply(lambda column: column.str.casefold().str.contains(needle, regex=False)).any(axis=1)
        shown = table[matches]
    st.dataframe(shown, hide_index=True, width="stretch", height=430, key=key,
                 column_config={column: st.column_config.Column(column, width="medium") for column in table.columns})
    st.caption(f"{len(shown):,} of {len(table):,} {noun} · {len(table.columns)} columns · Scroll horizontally to explore every field.")


def section_title(title, description, badge):
    st.markdown(f'<div class="section-heading"><div><h3>{html.escape(title)}</h3>'
                f'<p>{html.escape(description)}</p></div><span class="table-badge">{html.escape(badge)}</span></div>',
                unsafe_allow_html=True)


with st.sidebar:
    st.image(str(ROOT / "assets" / "anon-energy-logo.jpeg"), width=172)
    st.markdown('<p class="sidebar-tagline">Energy data. Shared with confidence.</p>', unsafe_allow_html=True)
    st.markdown('<div class="nav-label">YOUR WORKSPACE</div>', unsafe_allow_html=True)
    page = st.radio("Navigation", ["Data workspace", "Analytics dashboard"],
                    key="workspace_page", label_visibility="collapsed")
    st.markdown('<div class="sidebar-context"><span>LIVE SAMPLE</span><strong>Synthetic customer billing dataset</strong>'
                '<p>600 records · January–December 2025</p><small>Safe demonstration data only</small></div>', unsafe_allow_html=True)
    st.button("Reset dataset", on_click=reset_dataset, key="reset", icon=":material/restart_alt:", width="stretch")

st.markdown('<div class="workspace-topbar"><span>Anon Energy <b>/</b> Research workspace</span>'
            '<span class="mode-badge">Live sample</span></div>', unsafe_allow_html=True)

if page == "Analytics dashboard":
    st.markdown('<div class="workspace-intro"><span class="eyebrow">ENERGY INTELLIGENCE</span>'
                '<h1>Understand <em>your energy.</em></h1>'
                '<p>Consumption, forecasts and performance, together in one dashboard.</p></div>', unsafe_allow_html=True)
    render_analytics(demo)
else:
    raw = source_table(SAMPLE.read_bytes())
    ready = demo.protected is not None
    st.markdown('<div class="workspace-intro"><span class="eyebrow">DATA WORKSPACE</span>'
                '<h1>Your data. <em>Ready for research.</em></h1>'
                '<p>Explore the original dataset. Prepare a protected version. Keep the insight.</p></div>', unsafe_allow_html=True)

    action, description, next_step = st.columns([1.25, 2, 1.15], vertical_alignment="center")
    with action:
        prepare_clicked = st.button("Prepare research dataset", type="primary", key="prepare", width="stretch",
                                     icon=":material/shield:")
    with description:
        st.caption("Remove identifying details, pseudonymize IDs and apply bounded ±1% consumption adjustments.")
    with next_step:
        st.button("Open analytics", key="open_analytics", disabled=not ready, on_click=navigate,
                  args=("Analytics dashboard",), width="stretch", icon=":material/monitoring:")
    if prepare_clicked:
        try:
            with st.spinner("Preparing the research dataset…"):
                demo.prepare(SAMPLE.read_bytes())
            st.session_state.dataset_view = "Protected dataset"
            st.rerun()
        except DemoError as error:
            st.error(str(error))

    checks = demo.checks() if ready else {}
    a, b, c, d = st.columns(4)
    a.metric("Source records", f"{len(raw):,}")
    a.caption("12 months of customer billing")
    b.metric("Dataset columns", "41 → 14" if ready else "41")
    b.caption("Research output ready" if ready else "Original source fields")
    c.metric("Consumption adjustment", "±1%")
    c.caption("Bounded to each original reading")
    d.metric("Privacy checks", "Passed" if ready else "Not run")
    d.caption(f"Smallest checked group: {checks['minimum_people_per_selected_group']} people" if ready else "Run preparation to validate")

    original, protected, comparison = st.tabs(["Original dataset", "Protected dataset", "Compare changes"],
                                              key="dataset_view", on_change="rerun")
    with original:
        section_title("Original dataset", "Customer billing data, before preparation.", "SOURCE · 41 COLUMNS")
        st.caption(SAMPLE.name)
        show_table(raw, key="source")
    with protected:
        section_title("Protected dataset", "The research table, with identifying detail reduced.", "RESEARCH · 14 COLUMNS")
        if ready:
            show_table(demo.protected, key="protected")
            st.download_button("Download research dataset", demo.protected.to_csv(index=False).encode("utf-8-sig"),
                               "anon-energy-research.csv", mime="text/csv", key="download_research", icon=":material/download:")
            with st.expander("Preparation details"):
                st.write(f"{checks['research_rows']:,} rows retained · {checks['rows_withheld']} rows withheld · "
                         f"{checks['identifier_leaks_found']} configured raw-identifier matches.")
                st.write("Names, contact details, addresses and payment fields are removed. Bill, account and meter IDs are keyed hashes. Small locations are generalized and selected groups require at least five distinct people.")
                st.write("Each numeric day/night reading stays within ±1% of its original value, with stable per-bill adjustments and decimal rounding. Group protection can still suppress readings. Known consumption can link records; these checks do not establish complete anonymity.")
        else:
            st.markdown('<div class="empty-panel"><span class="empty-symbol">◇</span><h3>A protected version starts here.</h3>'
                        '<p>Prepare the dataset to see the 14 retained research columns.</p></div>', unsafe_allow_html=True)
    with comparison:
        section_title("What changed", "Follow each transformation from source to research data.", "OWNER VIEW")
        if ready:
            summary, consumption = st.tabs(["Column transformations", "Consumption adjustments"])
            with summary:
                from demo_policy import load_preview_policy
                policy = load_preview_policy()
                explanations = {"delete": "Removed", "keyed_hash": "Keyed hash", "keep_time": "Billing period retained",
                                "keep": "Retained; group rules may generalize", "year_only": "Generalized to decade",
                                "clean_location": "Cleaned; small locations generalized", "keep_if_min_group": "Retained only for safe groups",
                                "merge_values": "Sensitive categories generalized", "bounded_noise": "Stable adjustment within ±1%"}
                mapping = pd.DataFrame([{"Source column": column, "Research column": rule.output or "—",
                                         "Treatment": explanations.get(rule.action, rule.action)}
                                        for column, rule in policy.rules.items()])
                st.dataframe(mapping, hide_index=True, width="stretch", height=430, key="column_mapping")
            with consumption:
                changes = demo.consumption_changes()
                st.caption("Owner-only comparison. Original readings and percentage changes are excluded from the research download.")
                st.dataframe(changes, hide_index=True, width="stretch", height=430, key="noise_changes",
                             column_config={name: st.column_config.NumberColumn(name, format="%.4f%%", width="medium")
                                            if name.endswith("_pct") else st.column_config.Column(name, width="medium")
                                            for name in changes.columns})
        else:
            st.info("Prepare the dataset to compare the original and protected values.")

st.markdown('<div class="workspace-footer"><strong>Anon Energy</strong><span>Energy data. Shared with confidence.</span></div>',
            unsafe_allow_html=True)
