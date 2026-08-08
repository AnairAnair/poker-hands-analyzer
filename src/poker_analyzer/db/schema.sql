-- Poker Hand Analyzer - SQLite schema
--
-- Three normalized tables, per the project spec's storage section:
--   sessions -> hands -> actions  (one-to-many, one-to-many)
--
-- This file defines structure only. No data is loaded here - ingestion of
-- synthetic or real hands is a later session's work.
--
-- Money/stakes are stored in big blinds (REAL) rather than dollars, since bb
-- is the unit win-rate and EV analysis will actually use. Session buy-in/
-- cash-out are stored in cents (INTEGER) to avoid floating point rounding.

PRAGMA foreign_keys = ON;

-- One row per live session played.
CREATE TABLE IF NOT EXISTS sessions (
    session_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date    TEXT NOT NULL,              -- ISO 8601 date, e.g. '2026-08-05'
    location        TEXT NOT NULL,              -- e.g. 'The Brook'
    game_type       TEXT NOT NULL DEFAULT 'NLHE',
    stakes          TEXT NOT NULL,              -- e.g. '1/3', '2/5'
    big_blind_cents INTEGER NOT NULL,           -- big blind size in cents, e.g. 300 = $3
    buy_in_cents    INTEGER NOT NULL,
    cash_out_cents  INTEGER,                    -- NULL until the session is closed out
    start_time      TEXT,                       -- ISO 8601 timestamp
    end_time        TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per hand played, linked to its session.
CREATE TABLE IF NOT EXISTS hands (
    hand_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    hand_number          INTEGER NOT NULL,       -- sequence number within the session, starts at 1
    hero_position        TEXT NOT NULL,          -- BTN, SB, BB, UTG, UTG+1, MP, HJ, CO, ...
    hero_hole_cards       TEXT NOT NULL,          -- e.g. 'Ah Kd'
    num_players           INTEGER NOT NULL,       -- players dealt in at hand start
    effective_stack_bb    REAL,                   -- hero's effective stack at hand start, in bb
    board_flop            TEXT,                   -- e.g. 'Jh 8h 3c'; NULL if hand ended preflop
    board_turn            TEXT,                   -- single card, e.g. '2c'
    board_river           TEXT,                   -- single card, e.g. '9d'
    pot_size_bb           REAL,                   -- final pot size, in bb
    result_bb             REAL,                   -- hero's net result for the hand, in bb (+/-)
    went_to_showdown      INTEGER NOT NULL DEFAULT 0 CHECK (went_to_showdown IN (0, 1)),
    notes                 TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, hand_number)
);

-- One row per action taken during a hand (by any player at the table hero can see the action for).
CREATE TABLE IF NOT EXISTS actions (
    action_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    hand_id         INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    street          TEXT NOT NULL CHECK (street IN ('preflop', 'flop', 'turn', 'river')),
    action_order     INTEGER NOT NULL,           -- 1-based order of this action within the street
    actor_position   TEXT NOT NULL,              -- BTN, SB, BB, UTG, ...
    action_type      TEXT NOT NULL CHECK (action_type IN ('fold', 'check', 'call', 'bet', 'raise')),
    amount_bb        REAL,                       -- size of the bet/raise/call, in bb (NULL for fold/check)
    pot_before_bb    REAL,                       -- pot size immediately before this action, in bb
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (hand_id, street, action_order)
);

CREATE INDEX IF NOT EXISTS idx_hands_session_id ON hands(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_hand_id ON actions(hand_id);
CREATE INDEX IF NOT EXISTS idx_actions_hand_street ON actions(hand_id, street);
