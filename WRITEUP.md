# Poker Hand Analyzer

## Why I built this

I've played poker for years, mostly live cash games at 1/2, plus home games and online. This summer I wanted to measure something I'd always just played by feel. Every decision at the table is a bet under uncertainty, which is basically what drew me to finance in the first place, so I built a tool that takes real hands I've played and calculates the expected value of the actual decisions I made, not just whether I won or lost the pot.

## What it does

The tool ingests hand histories through a validated pipeline into a Postgres (Supabase) database, currently 64 real hands across 7 sessions, both live and online, at two different stake levels. From there, an EV engine estimates an opponent's likely range at each decision point using the Chen formula, calculates my equity against that range, and accounts for fold equity, since a bet that just wins the pot outright is worth something a pure showdown-equity model misses entirely. Range estimation narrows street by street postflop as opponents actually act, not just once preflop. On top of that sits a win rate and variance tracker broken out by stake level, a leak-detection layer that groups decisions by position and action type to surface recurring patterns rather than grading hands one at a time, a command-line report, and a dashboard.

## A finding the tool actually surfaced

Across 10 separate hands, every single one of my UTG opening raises landed in the exact same bucket: not losing, not winning, sitting right on the fence. Ten independent hands clustering on the same line isn't random scatter, it's a real signal, and it points to one of two things. Either my UTG range is a touch too loose for these particular tables, a real leak worth fixing. Or the model is underselling these raises, since it estimates fold frequency using a theoretical, game-theory-optimal benchmark, and the actual players at my stakes fold less than that benchmark assumes, which the tool would have no way of knowing on its own. I don't know yet which explanation is right, but that's the honest, useful place this kind of analysis is supposed to land, a specific question worth thinking about at the table, not a vague sense that something might be off.

## How it actually got built

I used Claude Code to build this, but not by asking for the whole thing at once. Each session had a tight, written scope, one real piece of functionality, with everything else explicitly out of bounds until its own session. That mattered more than I expected going in.

A few things stood out. When there was a genuine design decision buried in a build, like how to model an opponent's range, or how to estimate fold frequency, I had it stop and ask rather than pick silently, then I made the actual call myself. When it reported something as done, I checked it before trusting it, and more than once the report didn't match reality, a fix that looked complete but was still sitting as an unaccepted suggestion, a test that had quietly started hitting a real database instead of a mock one. I also built an automated structural check for a large batch of hand data I logged, catching real errors, a mis-transcribed card suit, two dropped actions, a mismatched position label, before any of it reached the actual database. Catching that kind of gap between what a tool says it did and what it actually did feels like a useful discipline on its own, not just a poker-project skill.

## A couple of decisions worth walking through

**How to model an opponent's range.** The straightforward option was to hand-curate a published opening-range chart. I went with scoring all 169 starting hands using the Chen formula instead, then taking a percentile cutoff that varies by position and action type. It's a heuristic, not solved poker, but it scales to every action type in real hand data, opens, calls, 3-bets, from one formula, instead of needing a separate hand-curated chart for each one.

**How to estimate fold equity.** I used minimum defense frequency, a real poker-theory result for the fraction of hands an opponent needs to continue with to stop a bet from being automatically profitable, and used its complement as a fold-frequency estimate. It's a real result, but it's a GTO benchmark, not a read on any actual opponent, and at the live, recreational stakes this data comes from, people generally fold less than a GTO-optimal frequency suggests. That exact gap is the leading explanation for the UTG finding above, not a coincidence, the model's own documented limitation showing up in real output. I wrote that down as a known limitation rather than letting the numbers imply more confidence than they'd earned. That distinction, a model being honestly incomplete versus quietly wrong, is the same distinction that matters in any financial model.

## What's next

I'm building a voice-logging companion next, since typing every hand into a structured format after a session is the most tedious part of actually using this. It listens to a hand being talked through out loud and extracts it into the same format, asking a clarifying question when something's actually ambiguous instead of guessing wrong, which is exactly the problem I ran into logging hands by voice myself.
