# Poker Hand Analyzer

A lightweight tool for live cash game players to log hands by hand, calculate the
expected value of individual decisions, and aggregate win rate and variance across
sessions.

> Status: early scaffolding. Ingestion, EV engine, decision flagging, and session
> aggregation are not built yet - see "Current status" below.

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
│       └── hand_log_template_GUIDE.md  # documents every column/encoding
├── src/
│   └── poker_analyzer/
│       ├── db/
│       │   ├── schema.sql            # sessions / hands / actions tables
│       │   └── init_db.py            # creates a SQLite DB from schema.sql
│       └── equity/
│           └── calculator.py         # treys-based equity calculator
├── scripts/                # CLI entry points (not built yet)
└── tests/
    └── test_equity_calculator.py
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

## Equity calculator

`src/poker_analyzer/equity/calculator.py` wraps `treys` to compute heads-up equity
between two starting hands, given 0, 3, or 4 known board cards. It's validated in
`tests/test_equity_calculator.py` against known textbook spots. Run the tests with:

```bash
pytest
```

## Current status

Built so far (this session):

- [x] Project scaffolding
- [x] SQLite schema (sessions, hands, actions) - structure only
- [x] CSV hand-log template + guide
- [x] Equity calculator, validated against known spots

Not built yet (later sessions, per the project spec's build phases):

- [ ] Synthetic/real hand data ingestion pipeline
- [ ] EV engine (decision-level EV vs. alternatives, plus/minus/marginal flagging)
- [ ] Session aggregation (win rate in bb/100, variance, std dev, up/downswing tracking)
- [ ] CLI reports
- [ ] Dashboard (pandas + matplotlib, or Streamlit)
- [ ] Portfolio writeup

## License

TODO - add before making the repo public.
