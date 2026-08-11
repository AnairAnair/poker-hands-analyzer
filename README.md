# Poker Hand Analyzer

A lightweight tool for live cash game players to log hands by hand, calculate the
expected value of individual decisions, and aggregate win rate and variance across
sessions.

> Status: early scaffolding. Preflop-only EV engine, decision flagging, session
> aggregation stats, a unified CLI, and a Streamlit dashboard are built; postflop EV
> and fold equity modeling are not - see "Current status" below.

## Why this project

TODO (Week 4 portfolio writeup): expand on the finance-skills framing from the spec
(EV under uncertainty, variance/drawdown tracking, building a repeatable analytical
framework from messy input) once the tool is far enough along to demo.

## Tech stack

- Python 3.11+
- [pandas](https://pandas.pydata.org/) for data manipulation and aggregation
- SQLite (stdlib `sqlite3`) for persistent storage
- [treys](https://github.com/ihendley/treys) for hand evaluation and equity calculation
- [Typer](https://typer.tiangolo.com/) for the CLI
- [pytest](https://docs.pytest.org/) for tests
- [Streamlit](https://streamlit.io/) (with [Altair](https://altair-viz.github.io/) for
  charts) for the dashboard

## Project layout

```
poker-hand-analyzer/
├── README.md
├── requirements.txt
├── pyproject.toml          # pytest config (testpaths, pythonpath)
├── data/
│   └── templates/
│       ├── hand_log_template.csv     # exact column format for logging hands
│       ├── hand_log_template_GUIDE.md  # documents every column/encoding
│       └── real_hands.csv            # real logged hands, loaded into the DB
├── src/
│   └── poker_analyzer/
│       ├── cli.py                    # unified Typer CLI: validate / ingest / stats / ev-report
│       ├── db/
│       │   ├── schema.sql            # sessions / hands / actions tables
│       │   └── init_db.py            # creates a SQLite DB from schema.sql
│       ├── ingestion/
│       │   └── loader.py             # CSV -> validated -> loaded into SQLite
│       ├── equity/
│       │   └── calculator.py         # treys-based equity calculator
│       ├── ev/
│       │   ├── ranges.py             # Chen-formula opponent range assignment
│       │   └── engine.py             # preflop decision-level EV + +EV/-EV/marginal flagging
│       ├── stats/
│       │   └── aggregator.py         # per-session + combined win rate, variance, swings
│       └── dashboard/
│           ├── data_prep.py          # shapes aggregator/ev-engine output into DataFrames
│           └── app.py                # Streamlit app - renders data_prep's output, no logic
├── scripts/
│   └── poker_cli.py                  # executable entry point for src/poker_analyzer/cli.py
└── tests/
    ├── test_cli.py
    ├── test_equity_calculator.py
    ├── test_hand_log_validator.py
    ├── test_ingestion.py
    ├── test_ev_engine.py
    ├── test_stats_aggregator.py
    └── test_dashboard_data_prep.py
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Database

Create a local SQLite database from the schema (structure only, no data):

```bash
python3 src/poker_analyzer/db/init_db.py --db poker_hands.db
```

See `src/poker_analyzer/db/schema.sql` for the full schema and comments.

## Logging hands

Use `data/templates/hand_log_template.csv` as the format for manually logging hands
from live play. Read `data/templates/hand_log_template_GUIDE.md` first - it documents
every column and the encoding used for actions-by-street and cards.

## CLI

Everything below - validating a CSV, ingesting it, and reporting stats/EV - is
available as subcommands of one Typer CLI, `scripts/poker_cli.py`. Each subcommand
is a thin wrapper: all the actual logic lives in the modules under
`src/poker_analyzer/` (validation, ingestion, stats, ev), so the CLI has nothing to
duplicate or drift out of sync with.

```bash
python scripts/poker_cli.py --help

python scripts/poker_cli.py validate data/templates/hand_log_template.csv
python scripts/poker_cli.py ingest data/templates/real_hands.csv --db poker_hands.db
python scripts/poker_cli.py stats --db poker_hands.db
python scripts/poker_cli.py ev-report --db poker_hands.db
```

| Subcommand  | Wraps                                  | Key options |
|-------------|-----------------------------------------|--------------|
| `validate`  | `validation/validator.py`               | `csv_path` |
| `ingest`    | `ingestion/loader.py`                   | `csv_path`, `--db`, `--buy-in-cents` |
| `stats`     | `stats/aggregator.py`                   | `--db` |
| `ev-report` | `ev/engine.py`                          | `--db`, `--trials`, `--seed` |

Run `python scripts/poker_cli.py <subcommand> --help` for full option details.
Tested end-to-end (and with each subcommand's dispatch to its underlying module
verified independently of the real logic) in `tests/test_cli.py`.

## CSV validator

Run the validator against a hand-log CSV to check the structure and field formats
before using it downstream:

```bash
python scripts/poker_cli.py validate data/templates/hand_log_template.csv
```

It reports row/column-specific errors for missing columns, bad dates, malformed card
fields, invalid position codes, malformed action strings, and blank street fields
where a street should not be empty.

## Equity calculator

`src/poker_analyzer/equity/calculator.py` wraps `treys` to compute heads-up equity
between two starting hands, given 0, 3, or 4 known board cards. It's validated in
`tests/test_equity_calculator.py` against known textbook spots. Run the tests with:

```bash
pytest
```

## Preflop EV engine

`src/poker_analyzer/ev/` computes decision-level EV for hero's preflop actions
(fold / check / call / bet / raise) already loaded into the database:

- `ev/ranges.py` scores all 169 starting hands with the Chen formula and turns a
  percentile cut over that scale into an opponent's range (e.g. "top 10%"). See the
  module docstring for why this approach was chosen over a curated per-position range
  chart.
- `ev/engine.py` reconstructs the pot and hero's cost from the logged preflop action
  sequence (blinds aren't logged as actions, so this assumes the standard SB=0.5bb /
  BB=1.0bb posts), assigns the opposing range for hero's specific spot, computes hero's
  equity against it with the existing equity calculator, and compares a static EV of
  hero's action (assuming a check to showdown from here - no fold equity, no postflop
  betting modeled) against EV(fold) = 0, flagging the decision `+EV`, `-EV`, or
  `marginal`. See the module docstring for the full reasoning and known limitations.

```python
from poker_analyzer.ev.engine import analyze_all_preflop_decisions

for decision in analyze_all_preflop_decisions("poker_hands.db"):
    print(decision.hand_id, decision.action_type, decision.hero_equity_pct, decision.flag)
```

Until now this was the only way to see the engine's output - it was returned from a
function call and never printed anywhere. `poker_cli.py ev-report` prints it per hand:
hero's position, the action taken, the `+EV`/`-EV`/`marginal` flag, and the equity/EV
numbers behind it (folds print as `baseline` - no EV is computed for a $0-cost action):

```bash
python scripts/poker_cli.py ev-report --db poker_hands.db

# Poker Hand Analyzer - Preflop EV Report
# ============================================
#
# Hand 6 (session 1) - hero: CO
#   raise 7.50bb     flag: +EV      equity:  78.3%  EV(action): +7.76bb  EV(fold): +0.00bb  diff: +7.76bb
```

`--trials` and `--seed` control the Monte Carlo equity simulation (trials per opponent
range combo, and a seed for reproducible runs - see `tests/test_cli.py` and
`tests/test_ev_engine.py` for seeded examples).

Validated in `tests/test_ev_engine.py` against real hands from `data/templates/real_hands.csv`
and a couple of textbook preflop spots. Postflop EV, multiway equity, and fold-equity
modeling are all explicitly out of scope for this pass - see the module docstrings.

## Session aggregation stats

`src/poker_analyzer/stats/aggregator.py` computes, per-session and combined across
every session in the database: total hands, total result in bb, win rate in bb/100
(the standard poker convention), sample variance/standard deviation of per-hand
results, and a peak-to-trough/trough-to-peak "biggest downswing"/"biggest upswing"
metric (not just the single best/worst hand - see the module docstring). Validated
in `tests/test_stats_aggregator.py` against the real 15 hands now in the database
(cross-checking sums, `statistics.variance`, and a brute-force swing reference) plus
a hand-verifiable constructed sequence.

```bash
python scripts/poker_cli.py stats --db poker_hands.db
```

With only 15 hands loaded, none of these numbers - especially bb/100 - are
statistically meaningful yet; the command prints that caveat alongside the numbers.
This is a correctness check on the calculation pipeline, not a performance read.

## Dashboard

`src/poker_analyzer/dashboard/` is a Streamlit app that visualizes the same data the
CLI prints. It computes nothing itself: `dashboard/data_prep.py` shapes DataFrames by
calling straight into `stats/aggregator.py` and `ev/engine.py` (the same
wrap-don't-duplicate principle as `cli.py`), and `dashboard/app.py` only renders what
`data_prep.py` returns.

Launch it from the project root (after `pip install -r requirements.txt`, which now
includes Streamlit):

```bash
streamlit run src/poker_analyzer/dashboard/app.py
```

This opens the dashboard in your browser at `http://localhost:8501`. The sidebar has
the database path (defaults to `poker_hands.db`) and the equity-simulation trial
count/seed used for the preflop EV breakdown - lower the trial count for a faster,
noisier load.

The page has three sections:

- **Result over time** - cumulative bb result and running bb/100 win rate across
  hands, with a session filter (cumulative totals restart at 0 for a single session,
  matching how `aggregate_session_stats` scopes its own swing math independently per
  session - see `data_prep.results_over_time`'s docstring for why filtering a
  combined series would give the wrong number).
- **Preflop decisions: +EV / -EV / marginal** - a count/percentage breakdown of every
  logged preflop decision by `ev/engine.py`'s flag, plus the full per-decision detail
  table (position, action, equity, EV numbers).
- **Per-session summary** - one row per session plus a combined row, straight from
  `aggregate_all` (hands, result, win rate, variance, std dev, swings).

As with `ev-report`, the preflop breakdown re-runs the Monte Carlo equity simulation
on load, so it takes a few seconds with the default trial count.

`dashboard/data_prep.py`'s data-shaping functions are covered in
`tests/test_dashboard_data_prep.py`, cross-checked against the same real 15-hand
database the other modules' tests use. `dashboard/app.py` itself (the Streamlit
rendering) isn't unit-tested the same way - verified instead by running the app
against the real database and checking each section against the CLI's
`stats`/`ev-report` output.

## Current status

Built so far:

- [x] Project scaffolding
- [x] SQLite schema (sessions, hands, actions) - structure only
- [x] CSV hand-log template + guide
- [x] CSV validator for hand-log files, with pytest coverage
- [x] Equity calculator, validated against known spots
- [x] Hand data ingestion pipeline (CSV -> validated -> loaded into SQLite), with pytest coverage
- [x] 15 real hands loaded into the database, across 2 sessions
- [x] Preflop EV engine: decision-level EV vs. folding, +EV/-EV/marginal flagging,
      Chen-formula opponent range assignment - validated against real hands and
      textbook spots
- [x] Session aggregation (win rate in bb/100, variance, std dev, up/downswing
      tracking) - per-session and combined, validated against the 15 real hands
- [x] Unified CLI (`scripts/poker_cli.py`, Typer) with `validate` / `ingest` /
      `stats` / `ev-report` subcommands, wrapping the modules above rather than
      duplicating their logic - with pytest coverage of argument parsing, dispatch,
      and end-to-end runs
- [x] `ev-report`: the first place the preflop EV engine's output is actually
      printed anywhere (position, action, +EV/-EV/marginal flag, equity/EV numbers)
- [x] Streamlit dashboard (`dashboard/app.py`) - result-over-time and win-rate
      charts, preflop +EV/-EV/marginal breakdown, per-session summary table, all
      wrapping `stats/aggregator.py` and `ev/engine.py` rather than recomputing;
      data-shaping layer (`dashboard/data_prep.py`) covered by pytest

Not built yet (later sessions, per the project spec's build phases):

- [ ] Postflop EV engine (needs its own dedicated design work - multiway equity,
      board texture, bet sizing all change the range-assignment approach)
- [ ] Fold equity modeling
- [ ] Portfolio writeup

## License

TODO - add before making the repo public.
