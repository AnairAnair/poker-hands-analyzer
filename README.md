# Poker Hand Analyzer

A lightweight tool for live cash game players to log hands by hand, calculate the
expected value of individual decisions, and aggregate win rate and variance across
sessions.

> Status: early scaffolding. Preflop-only EV engine, decision flagging, and session
> aggregation stats are built; postflop EV, CLI reports, and the dashboard are not -
> see "Current status" below.

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
- Streamlit - optional, later phase, for a dashboard

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
│       └── stats/
│           └── aggregator.py         # per-session + combined win rate, variance, swings
├── scripts/
│   ├── ingest_hand_log.py            # CLI: ingest a hand-log CSV into the DB
│   └── print_stats_summary.py        # prints the session aggregation stats to the terminal
└── tests/
    ├── test_equity_calculator.py
    ├── test_hand_log_validator.py
    ├── test_ingestion.py
    ├── test_ev_engine.py
    └── test_stats_aggregator.py
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

## CSV validator

Run the validator against a hand-log CSV to check the structure and field formats
before using it downstream:

```bash
python src/poker_analyzer/validation/validator.py data/templates/hand_log_template.csv
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
python scripts/print_stats_summary.py
```

With only 15 hands loaded, none of these numbers - especially bb/100 - are
statistically meaningful yet; the script prints that caveat alongside the numbers.
This is a correctness check on the calculation pipeline, not a performance read.

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
      tracking) - per-session and combined, validated against the 15 real hands,
      plus a terminal summary script

Not built yet (later sessions, per the project spec's build phases):

- [ ] Postflop EV engine (needs its own dedicated design work - multiway equity,
      board texture, bet sizing all change the range-assignment approach)
- [ ] CLI reports (a real CLI with flags/arguments - today's stats summary script
      just runs and prints)
- [ ] Dashboard (pandas + matplotlib, or Streamlit)
- [ ] Portfolio writeup

## License

TODO - add before making the repo public.
