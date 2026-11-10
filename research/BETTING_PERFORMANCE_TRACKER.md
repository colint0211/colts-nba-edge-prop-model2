# Betting Performance Tracker

This tracker is shared by the NBA player-prop and ATS workflows. It consumes normalized CSV exports and writes an append-only event ledger. It does not alter model qualification logic or production predictions.

## Canonical CSV input

Required headers:
- `play_id`: stable, unique ID from the source system
- `model`: e.g. `nba_player_props` or `nba_ats`
- `qualified_at`: ISO-8601 timestamp with timezone
- `sportsbook`
- `line`
- `odds_american`
- `model_probability`: decimal from 0 to 1

Recommended headers:
- `market`, `selection`, `result` (WIN/LOSS/PUSH/PENDING)
- `net_units` (if omitted for settled plays, derived from American odds)
- `source_record_id`, `qualification_rule_version`, `verification_status`

Rows with missing or unverifiable information are retained and flagged; they are never silently dropped. Do not invent a timestamp, book, line, odds, probability, or source ID to make a record look complete.

## Commands

```bash
python scripts/betting_performance_tracker.py ingest exports/player_props.csv --model nba_player_props
python scripts/betting_performance_tracker.py ingest exports/ats.csv --model nba_ats
python scripts/betting_performance_tracker.py report
python scripts/betting_performance_tracker.py settle PLAY_ID WIN
```

Default ledger: `data/performance_events.csv`. Keep this file in durable storage (database/object storage or a versioned private data store); do not rely on an ephemeral deployment filesystem.

## Accounting rules

- Baseline stake is exactly 1 unit risk per play.
- American odds determine net profit on a win: +odds/100 for positive odds, +100/abs(odds) for negative odds; loss = -1; push = 0.
- Win rate excludes pushes; pushes remain in the play count and ledger.
- ROI = total net units / total units risked.
- Cumulative profit and maximum peak-to-trough drawdown use settled plays in qualification-time order.
- Pending plays count as qualifying plays but not as settled/risked plays in ROI.
- A supplied `net_units` is preserved; if absent for a settled play, the tracker derives it from the recorded odds and flags that derivation.

## Immutability and audit

The ledger is event-sourced:
- `QUALIFIED` event is appended once per `play_id`; duplicate imports are skipped.
- `SETTLED` event is appended once; an existing settlement cannot be overwritten through this CLI.
- Losing plays are never deleted. Do not reuse IDs.
- Qualification criteria are not stored as mutable live settings: every row records `qualification_rule_version`. Preserve each version's definition in source control.
- Missing source IDs, timestamps, criteria versions, and invalid odds/probabilities are explicitly flagged.
- Historical qualification criteria must not be changed retroactively. Any future criteria change requires a new version and a new prospective cohort.

## Database option

The event schema maps directly to a database table with a unique constraint on `(event_type, play_id)`, append-only insert permissions, and separate read-only reporting access. For production reliability, use a durable database rather than a local Vercel filesystem. The current implementation accepts CSV exports; it does not claim a live database connection.

## Important interpretation

A tracker cannot reconstruct picks that were never logged at qualification time. Before reporting historical performance, reconcile source exports against the original prediction ledger and sportsbook snapshots. Missing/unverifiable records remain visibly flagged; report coverage alongside performance.