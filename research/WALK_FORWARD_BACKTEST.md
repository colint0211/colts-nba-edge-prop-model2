# Walk-Forward NBA ATS Backtest

This is an experimental, expanding-window walk-forward protocol. It is separate from the frozen away-only candidate and does not merge or deploy it.

## Protocol
- Sort by Eastern game date and prediction timestamp.
- For each consecutive test block (default 7 distinct NBA game dates), train only on games dated strictly before that block.
- Refit the ten-feature logistic model on the expanding historical sample. Pushes are excluded from binary model fitting but remain in the full game ledger.
- Predict every game in the test block once. Do not refit within a block.
- The away-only rule is fixed before the run: bet AWAY only when predicted home-cover probability is at most 45%; otherwise pass. Do not select thresholds by inspecting test results.
- Continue forward through all dates. Earlier test games may become training data only for later folds, as they would in live operation.

## Required CSV columns
`date_et, game_id, scheduled_tipoff_et, prediction_timestamp_et, feature_available_at_et, odds_available_at_et, home_score, away_score, spread_home, home_odds_american, away_odds_american` plus all ten feature columns listed in `scripts/nba_ats_walk_forward.py`.

`spread_home` uses the conventional home handicap sign (e.g. home -4.5 is -4.5). The result is home ATS win if home margin + home spread is positive, away ATS win if negative, and push if zero. Odds must be actual prices captured no later than prediction time, at the same chosen sportsbook. All timestamps must include timezone offsets.

The shared `feature_available_at_et` is a conservative earliest-availability gate for the feature vector. If features have different source timestamps, prepare the dataset using the latest timestamp among all included features. A single timestamp does not prove provenance: retain source-level lineage in the data pipeline.

## Leakage and audit gates
The script stops on missing required values, duplicate game IDs, invalid odds, non-finite numbers, missing timezone offsets, features/prices timestamped after prediction, or predictions at/after scheduled tipoff. It never silently drops invalid records. Run the separate dataset audit first. Do not use final injury reports, actual minutes, postgame statistics, revised future ratings, or closing odds unless they were genuinely available by the prediction cutoff.

## Reporting
- Every out-of-sample game is written to `selection_ledger.csv`, including passes and all wins/losses/pushes.
- Model picks: wins, losses, pushes, win rate excluding pushes, flat-unit net profit, ROI on one unit risked per bet, and maximum peak-to-trough drawdown.
- Probability metrics across all non-push test games: Brier score, log loss, and reliability bins.
- Fold-level metrics plus an overall naive benchmark that always bets HOME ATS at the recorded contemporaneous price. This is a deliberately simple directional benchmark, not a claim that it is the strongest market baseline. This simple benchmark is deliberately transparent; add a closing-market benchmark only when a valid, time-stamped closing line is available.
- No bets are silently removed. A push is recorded and returns zero net units. Voids are not inferred from scores; provide an explicit pre-set void policy and source fields before including voided events.

## Run
```bash
python scripts/nba_ats_walk_forward.py path/to/point_in_time_ats.csv
python scripts/nba_ats_walk_forward.py path/to/point_in_time_ats.csv --min-train 100 --test-days 7 --away-threshold 0.45
```

## Anti-overfitting policy
Freeze features, model family, threshold, odds source, cutoff, and settlement rules before the prospective test. Use validation data for development; reserve a later untouched period for final confirmation. If any choice changes after looking at test results, that period becomes development data and a new future holdout is required. Report all attempts, not only the best one. Never describe a backtest as proof of future profitability.
