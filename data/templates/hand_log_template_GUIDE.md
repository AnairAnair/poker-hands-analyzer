# Hand log template guide

This documents `hand_log_template.csv` - the exact format for manually logging hands
from live play. The two rows in that file are placeholders (clearly marked
`DUMMY DATA` in the `notes` column) that exist only to prove the format works. They
are not real hands.

There is no parser for this format yet - ingestion is out of scope for this session.
Log real hands into a copy of this CSV in whatever format matches this spec, so the
ingestion pipeline (built later) has a stable target to parse against.

## Columns

| Column | Type | Format | Example |
|---|---|---|---|
| `session_date` | date | `YYYY-MM-DD` | `2026-08-05` |
| `location` | text | free text | `The Brook` |
| `stakes` | text | `small/big` blinds in dollars | `1/3` |
| `hand_number` | integer | 1-based, resets each session | `1` |
| `num_players` | integer | players dealt in at hand start | `6` |
| `hero_position` | text | one of the position codes below | `CO` |
| `hero_hole_cards` | text | two cards, space-separated, card format below | `Ah Kd` |
| `effective_stack_bb` | number | hero's stack at hand start, in big blinds | `100` |
| `preflop_actions` | text | action string, format below | `UTG:fold>MP:fold>CO:raise2.5>BTN:fold>SB:fold>BB:call` |
| `flop_cards` | text | 3 cards, space-separated; blank if hand ended preflop | `Jh 8h 3c` |
| `flop_actions` | text | action string; blank if no flop | `BB:check>CO:bet4>BB:call` |
| `turn_card` | text | 1 card; blank if hand ended by/before the flop | `2c` |
| `turn_actions` | text | action string; blank if no turn | `BB:check>CO:bet9>BB:call` |
| `river_card` | text | 1 card; blank if hand ended by/before the turn | `9d` |
| `river_actions` | text | action string; blank if no river | `BB:check>CO:bet20>BB:fold` |
| `pot_size_bb` | number | final pot size, in big blinds | `39` |
| `result_bb` | number | hero's net result for the hand, in bb (can be negative) | `19` or `-1` |
| `went_to_showdown` | 0/1 | whether cards were shown at the end | `0` |
| `notes` | text | anything worth remembering about the hand | free text |

## Card format

Each card is `<rank><suit>`, no space between them:

- Ranks: `2 3 4 5 6 7 8 9 T J Q K A` (`T` = ten)
- Suits: `s` = spades, `h` = hearts, `d` = diamonds, `c` = clubs

Multiple cards in one field are space-separated: `Jh 8h 3c`. This matches the card
notation the `treys` equity library uses internally (see
`src/poker_analyzer/equity/calculator.py`), so hand history rows can be fed straight
into the equity calculator later without reformatting.

## Position codes

Use whichever of these apply to your table size: `UTG`, `UTG+1`, `MP`, `HJ`, `CO`,
`BTN`, `SB`, `BB`. For short-handed tables just use the subset that applies (e.g. a
6-max hand might only use `UTG`, `MP`, `CO`, `BTN`, `SB`, `BB`).

## Action string format

Each street's actions are one string: `POSITION:action[amount]`, chained with `>`,
in the order actions happened.

- `fold`, `check`, `call` take no amount: `BB:fold`, `BB:check`, `BB:call`
- `bet` and `raise` are followed immediately by the size in big blinds, no space:
  `CO:raise2.5`, `CO:bet4`
- Multiple actions are joined with `>`: `UTG:fold>MP:fold>CO:raise2.5>BB:call`

This keeps hand histories readable in a spreadsheet while remaining easy to parse
later (split on `>`, then split each token on `:`).

## What this template does NOT capture (by design, for now)

- Villain hole cards (only logged if they're shown at showdown - put that in `notes`
  for now; there's no dedicated column yet)
- Multiway side pots
- Antes / straddles (fold these into `notes` until there's a real need to model them)

These are deliberately out of scope until the ingestion pipeline and EV engine are
built and it's clear what they actually need.
