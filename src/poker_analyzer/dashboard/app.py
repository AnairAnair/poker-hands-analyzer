"""
Streamlit dashboard for the Poker Hand Analyzer.

This file renders; it does not calculate. Every number on the page comes from
`dashboard/data_prep.py`, which itself only wraps `stats/aggregator.py` and
`ev/engine.py` - same wrap-don't-duplicate principle as `cli.py`. Streamlit UI code
like this isn't unit-tested the way `data_prep.py` is (see its test file) - rendering
correctness needs a running app and a browser, not assertions - so this was verified
manually by running `streamlit run` against the real database and checking each
section against the CLI's `stats`/`ev-report` output. See the README's Dashboard
section for how to launch it.

Streamlit over static matplotlib charts: the dashboard needs several linked views
(a time series, a categorical breakdown, a summary table) re-read from the live
database on every run, plus basic interactivity (a session filter) - a single-file,
no-HTML app framework fits that better than generating and re-embedding static image
files for each view.

Reads from Postgres (SUPABASE_DB_URL, via db/connection.py) by default, same as the
CLI - see data_prep.py. The sidebar's "Local SQLite path" field is optional and only
useful for pointing at a local SQLite file (e.g. one built by a test fixture); leave
it blank for the normal Postgres-backed case.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import altair as alt
import pandas as pd
import streamlit as st

from poker_analyzer.dashboard.data_prep import (
    PREFLOP_FLAG_ORDER,
    preflop_decisions_dataframe,
    preflop_flag_counts,
    results_over_time,
    session_summary_table,
)
from poker_analyzer.ev.engine import DEFAULT_TRIALS_PER_COMBO

# Reserved status hues (not generic categorical colors) - +EV/-EV/marginal/baseline are
# quality judgments on a decision, not arbitrary series identities, so they get the
# status palette rather than a cycled categorical one.
FLAG_COLORS = {
    "+EV": "#0ca30c",  # good
    "-EV": "#d03b3b",  # critical
    "marginal": "#fab219",  # warning
    "baseline": "#898781",  # muted - a fold, no EV computed, not a quality judgment
}
LINE_COLOR = "#2a78d6"  # single-series categorical slot 1 (blue)
ZERO_LINE_COLOR = "#c3c2b7"

st.set_page_config(page_title="Poker Hand Analyzer", layout="wide")
st.title("Poker Hand Analyzer")

with st.sidebar:
    st.header("Settings")
    db_path_input = st.text_input(
        "Local SQLite path (optional)",
        value="",
        help="Leave blank to read from Postgres (SUPABASE_DB_URL), the normal case. "
        "Only set this to point at a local SQLite file instead.",
    )
    db_path = db_path_input or None
    trials = st.number_input(
        "EV equity trials per opponent combo",
        min_value=20,
        max_value=2000,
        value=DEFAULT_TRIALS_PER_COMBO,
        step=20,
        help="Monte Carlo trials per opponent-range combo for the preflop EV breakdown "
        "below. Lower = faster to load, noisier equity numbers.",
    )
    use_seed = st.checkbox(
        "Use fixed seed", value=True, help="Uncheck for a fresh random simulation each run."
    )
    seed = st.number_input("Seed", min_value=0, value=0, step=1, disabled=not use_seed)

if db_path is not None and not Path(db_path).exists():
    st.error(
        f"No local SQLite database found at `{db_path}`. Clear the sidebar field to "
        "read from Postgres instead, or point this at an existing SQLite file."
    )
    st.stop()

# --- Result over time ------------------------------------------------------------------

st.header("Result over time")

all_results_df = results_over_time(db_path)

if all_results_df.empty:
    st.info("No hands with a logged result yet.")
else:
    session_ids = sorted(all_results_df["session_id"].unique().tolist())
    session_choice = st.selectbox(
        "Session", ["All sessions"] + session_ids, help="Cumulative totals restart at 0 for a single session."
    )

    if session_choice == "All sessions":
        view_df = all_results_df.copy()
    else:
        view_df = results_over_time(db_path, session_id=session_choice)
    view_df["hand_seq"] = range(1, len(view_df) + 1)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Cumulative result (bb)")
        zero_line = (
            alt.Chart(pd.DataFrame({"y": [0]}))
            .mark_rule(color=ZERO_LINE_COLOR, strokeDash=[4, 4])
            .encode(y="y:Q")
        )
        result_line = (
            alt.Chart(view_df)
            .mark_line(color=LINE_COLOR, strokeWidth=2, point=alt.OverlayMarkDef(size=40, filled=True))
            .encode(
                x=alt.X("hand_seq:Q", title="Hand #"),
                y=alt.Y("cumulative_result_bb:Q", title="Cumulative bb"),
                tooltip=[
                    alt.Tooltip("hand_number:Q", title="Hand # (in session)"),
                    alt.Tooltip("session_date:N", title="Date"),
                    alt.Tooltip("result_bb:Q", title="Result (bb)", format="+.2f"),
                    alt.Tooltip("cumulative_result_bb:Q", title="Cumulative (bb)", format="+.2f"),
                ],
            )
        )
        st.altair_chart((zero_line + result_line).interactive(), width="stretch")

    with col2:
        st.subheader("Win rate over time (bb/100)")
        win_rate_line = (
            alt.Chart(view_df)
            .mark_line(color=LINE_COLOR, strokeWidth=2, point=alt.OverlayMarkDef(size=40, filled=True))
            .encode(
                x=alt.X("hand_seq:Q", title="Hand #"),
                y=alt.Y("cumulative_win_rate_bb_per_100:Q", title="Running bb/100"),
                tooltip=[
                    alt.Tooltip("hand_number:Q", title="Hand # (in session)"),
                    alt.Tooltip("cumulative_win_rate_bb_per_100:Q", title="bb/100", format="+.1f"),
                ],
            )
        )
        st.altair_chart(win_rate_line.interactive(), width="stretch")

    st.caption(
        f"Only {len(view_df)} hand(s) in this view - bb/100 win rate needs a much larger "
        "sample to be meaningful (see stats/aggregator.py's module docstring). These "
        "charts are here to visualize the calculation pipeline, not to make a read on "
        "real win rate yet."
    )

# --- Preflop decision breakdown ---------------------------------------------------------

st.header("Preflop decisions: +EV / -EV / marginal")

with st.spinner("Running the preflop EV engine (Monte Carlo equity simulation)..."):
    decisions_df = preflop_decisions_dataframe(
        db_path,
        trials_per_combo=int(trials),
        seed=int(seed) if use_seed else None,
    )

if decisions_df.empty:
    st.info("No preflop decisions found.")
else:
    counts_df = preflop_flag_counts(decisions_df)

    col1, col2 = st.columns([1, 1])

    with col1:
        breakdown_chart = (
            alt.Chart(counts_df)
            .mark_bar()
            .encode(
                x=alt.X("flag:N", sort=PREFLOP_FLAG_ORDER, title=None),
                y=alt.Y("count:Q", title="Decisions"),
                color=alt.Color(
                    "flag:N",
                    scale=alt.Scale(domain=list(FLAG_COLORS.keys()), range=list(FLAG_COLORS.values())),
                    legend=None,
                ),
                tooltip=["flag", "count", alt.Tooltip("pct:Q", format=".1f", title="% of decisions")],
            )
        )
        st.altair_chart(breakdown_chart, width="stretch")

    with col2:
        st.dataframe(
            counts_df.rename(columns={"flag": "Flag", "count": "Count", "pct": "% of decisions"}).set_index(
                "Flag"
            ),
            width="stretch",
        )

    st.subheader("Decision detail")
    detail_columns = {
        "hand_id": "Hand",
        "session_id": "Session",
        "hero_position": "Position",
        "action_type": "Action",
        "amount_bb": "Amount (bb)",
        "flag": "Flag",
        "hero_equity_pct": "Equity %",
        "ev_action_bb": "EV(action) bb",
        "ev_fold_bb": "EV(fold) bb",
        "ev_diff_bb": "EV diff bb",
    }
    st.dataframe(
        decisions_df[list(detail_columns)].rename(columns=detail_columns),
        width="stretch",
        hide_index=True,
    )

# --- Per-session summary -----------------------------------------------------------------

st.header("Per-session summary")

summary_df = session_summary_table(db_path)
summary_columns = {
    "label": "Session",
    "total_hands": "Hands",
    "total_result_bb": "Result (bb)",
    "win_rate_bb_per_100": "Win rate (bb/100)",
    "variance_bb": "Variance (bb^2)",
    "std_dev_bb": "Std dev (bb)",
    "biggest_upswing_bb": "Biggest upswing (bb)",
    "biggest_downswing_bb": "Biggest downswing (bb)",
}
st.dataframe(
    summary_df.drop(columns=["session_id"]).rename(columns=summary_columns),
    width="stretch",
    hide_index=True,
)
