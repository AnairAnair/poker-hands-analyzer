# Poker Hand Analyzer

A lightweight tool for live cash game players to log hands by hand, calculate the
expected value of individual decisions, and aggregate win rate and variance across
sessions.

> Status: early scaffolding. Preflop EV engine (with fold equity) and a postflop EV
> engine (range narrowing, no fold equity yet), decision flagging, session
> aggregation stats, a unified CLI, and a Streamlit dashboard are built - see
> "Current status" below.

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
│       │   └── engine.py             # preflop + postflop decision-level EV, +EV/-EV/marginal flagging
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
  BB=1.0bb posts), assigns the opposing range for hero's specific spot, and computes
  hero's EV for the action taken against EV(fold) = 0, flagging the decision `+EV`,
  `-EV`, or `marginal`. See the module docstring for the full reasoning and known
  limitations.

  For a check/call, that EV is a *static* showdown number: hero's equity against the
  full opponent range (via the equity calculator), as if the hand were checked down
  from here with no further betting - postflop betting is still out of scope.

  For a bet/raise, hero's action can actually fold villain out, so the EV splits into
  two branches: villain folds (hero wins the pot as it stood before hero's bet,
  uncontested) or villain continues (the same equity-vs-range showdown math, but now
  against only the sub-range that would realistically continue). The fold probability
  is estimated with **minimum defense frequency (MDF)**, a standard poker-theory
  result: facing a bet of size `B` into a pot of size `P`, a defender must continue
  with at least `P / (P + B)` of their range to avoid being exploitable by a bettor
  betting every hand, so this project uses MDF's complement, `B / (P + B)`, as the
  fold-frequency estimate (`estimate_fold_pct` in `ev/engine.py`). The villain combos
  that "fold" are modeled as the weakest slice of villain's already-assigned range
  band (`ev/ranges.py`'s bands are ordered strongest-to-weakest), so a bet trims that
  band down to a narrower "continuing" range before the same equity-vs-range
  calculation runs.

  This was a real design decision with more than one reasonable approach (decided
  with the user): the alternative was a static fold-% lookup table keyed by action
  type/position, similar in shape to `OPEN_RAISE_TOP_PCT`. MDF was chosen because it
  responds to hero's actual bet size (already reconstructed for every decision)
  rather than treating a min-raise and a pot-sized raise identically, and because it
  reuses the same percentile-band range representation already built in
  `ev/ranges.py` instead of adding a second, disconnected model.

  **Known limitation, explicitly:** MDF is an equilibrium/GTO benchmark, not a read
  on any real villain - it assumes they defend *exactly* enough to stay unexploitable.
  Real opponents, especially in the live-cash/informal-game context this project
  targets, very often over-fold (or occasionally under-fold) relative to MDF. Treat
  `fold_pct` as a principled first-pass estimate, not a tendency read. See the "Fold
  equity" section of `ev/engine.py`'s module docstring for the full writeup.

```python
from poker_analyzer.ev.engine import analyze_all_preflop_decisions

for decision in analyze_all_preflop_decisions("poker_hands.db"):
    print(decision.hand_id, decision.action_type, decision.hero_equity_pct, decision.flag)
    print(decision.fold_pct, decision.continuing_range_band)  # bet/raise only, else None
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

(`equity` above is hero's equity against whichever range actually feeds the EV number -
the full opponent band for a check/call, or the narrower post-fold-equity continuing
range for a bet/raise; `fold_pct`/`continuing_range_band` aren't in the CLI's printed
line yet, but are on every `PreflopDecision` for a bet/raise.)

`--trials` and `--seed` control the Monte Carlo equity simulation (trials per opponent
range combo, and a seed for reproducible runs - see `tests/test_cli.py` and
`tests/test_ev_engine.py` for seeded examples).

Validated in `tests/test_ev_engine.py` against real hands from `data/templates/real_hands.csv`,
a couple of textbook preflop spots (including a UTG pocket-aces open, which now clearly
flags `+EV` once fold equity is credited - it only came back `marginal` before), and
direct unit tests of the fold-equity math (`estimate_fold_pct`, `_continuing_band`) in
isolation. Multiway equity is still explicitly out of scope - see the module docstrings.

## Postflop EV engine

`ev/engine.py`'s `analyze_hand_postflop` extends the same decision-level EV analysis -
equity against an assigned villain range, compared to EV(fold) = 0, flagged `+EV` /
`-EV` / `marginal` - to hero's flop/turn/river actions. It shares the preflop engine's
percentile-band range representation and its `+EV`/`-EV`/`marginal` flagging, but
differs in two ways:

- **Range narrowing carries forward street by street** instead of being assigned fresh
  per decision. Villain's range enters the flop as whatever band the full preflop
  action sequence implies (the same range assignment `analyze_hand_preflop` uses), then
  narrows further every time an *opponent* bets, raises, or checks on a later street: a
  bet or raise narrows the band toward its strong end, using a fixed multiplier
  (`POSTFLOP_BET_NARROW_PCT` = 40%, `POSTFLOP_RAISE_NARROW_PCT` = 20%); a check applies
  only a small cap (`POSTFLOP_CHECK_NARROW_PCT` = 90%). This compounds across streets -
  an opponent betting both the flop and the turn narrows the band twice - and reuses
  `_continuing_band`, the same mechanic fold equity uses to shrink a band toward
  strength, applied here for a different purpose (narrowing villain's range from
  villain's own action, not splitting hero's bet into fold/continue branches). A call
  never narrows anything - it inherits whatever the last bet/raise/check already set -
  and hero's own actions never narrow the band either, since this estimates *villain's*
  range from *villain's* actions.

  This was a real design decision with more than one reasonable approach (decided with
  the user): the alternative was letting the narrowing amount also depend on board
  texture (a bet on a wet, coordinated board narrowing more than the same bet on a dry
  one). Fixed per-action-type multipliers were chosen instead, for the same reason MDF
  won out over a static fold-% lookup table for preflop fold equity: it reuses the
  existing percentile-band machinery directly with no new model to design or tune this
  session. Board-texture-aware narrowing is a reasonable follow-up, not ruled out.

- **Equity is computed against the actual board** at each street (3 cards on the flop,
  4 on the turn, 5 on the river), via `equity_vs_range`'s new optional `board`
  parameter, which passes straight through to the existing `calculate_equity` -
  already validated against partial boards (see `tests/test_equity_calculator.py`'s
  flush-draw-vs-overpair flop spot, reused directly in the postflop engine tests).
  Getting a fully-known river board working required a small extension to
  `calculate_equity` itself: it previously only accepted 0/3/4 board cards, but a river
  decision needs all 5 - the underlying exact-enumeration logic already handled this
  correctly (a complete board has exactly one possible "runout", the empty one), only
  the validation was too strict. Board cards are also excluded as dead cards alongside
  hero's own hole cards when enumerating villain's range combos.

**Postflop fold equity is deliberately not modeled this session** - a postflop
bet/raise's `ev_action_bb` is a static showdown number against the narrowed range,
exactly like a preflop check/call (or like every preflop decision before fold equity
was added). Preflop needed its own two-session split - the engine itself, then fold
equity on top of it - rather than doing both at once, and postflop follows the same
split for the same reason.

```python
from poker_analyzer.ev.engine import analyze_all_postflop_decisions

for decision in analyze_all_postflop_decisions("poker_hands.db"):
    print(decision.hand_id, decision.street, decision.board, decision.hero_equity_pct, decision.flag)
```

Validated in `tests/test_ev_engine.py`: real postflop hands from `data/templates/real_hands.csv`
(a flopped set of aces that stays `+EV` on every street, a rivered nut flush that's a
deterministic 100% equity lock, a postflop fold correctly flagged `baseline` with no
equity computed), a constructed textbook spot reusing the flush-draw-vs-overpair flop
example (Ah Kh vs. a tight, pair-heavy 3-bet range on Jh 8h 3c comes back a slight
equity favorite, same counterintuitive lesson as the single-hand example, now running
through the full range-narrowing pipeline), and direct unit tests of the range-narrowing
replay (`_reconstruct_postflop_street`) pinning down exactly which actions narrow the
band and by how much, independent of equity simulation.

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
- [x] Fold equity modeling for bet/raise decisions (MDF-derived fold-frequency
      estimate, continuing-range equity) - validated against real hands, a textbook
      spot, and direct unit tests of the fold-equity math
- [x] Postflop EV engine: decision-level EV for flop/turn/river actions, with
      villain's range narrowing street by street from postflop actions (fixed
      per-action-type multipliers, reusing the preflop percentile-band machinery)
      and equity computed against the actual board at each street - validated
      against real hands, a textbook spot, and direct unit tests of the
      range-narrowing replay. Not yet wired into `ev-report` or the dashboard,
      which still only display preflop decisions - see "Not built yet" below.
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

- [ ] Postflop fold equity (a postflop bet/raise folding out worse hands, the way
      preflop fold equity already works) - deliberately deferred this session, same
      reasoning as preflop's own engine-then-fold-equity split
- [ ] Board-texture-aware postflop range narrowing (the alternative considered and
      set aside in favor of fixed per-action-type multipliers - see "Postflop EV
      engine" above)
- [ ] Wiring postflop decisions into `ev-report` / the dashboard (both still only
      display preflop decisions)
- [ ] Leak-detection / pattern analysis across logged decisions
- [ ] Portfolio writeup

## License

TODO - add before making the repo public.
