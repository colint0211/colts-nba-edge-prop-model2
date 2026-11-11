# NBA Sides & Totals Model Audit — 2026-11-10

## Status

**Audit finding: the current game model is not ready to issue confident betting recommendations.** This is a code/data-pipeline diagnosis, not a claim about betting performance. No projection formula has been changed in this audit branch.

## Confirmed blockers in the current implementation

### 1. The fallback possession estimate uses the wrong rebound field
In `src/api/game_model.py`, `_tank01_ratings()` assigns `off["reb"]` and `defensiveStats["reb"]` to variables named `oreb` and `dreb`, then subtracts 25% of those values in the possession estimate. The source field is total rebounds in this code path, not a verified offensive-rebound count. Total rebounds cannot be substituted for offensive rebounds in the standard possession estimate. This contaminates estimated possessions, ORtg, DRtg, and pace.

**Required correction:** use a verified offensive-rebound field for the possession formula; if the source does not provide it, do not label the resulting estimate as a valid possession-based rating. Prefer official advanced team ratings or a correctly constructed box-score possession estimate.

### 2. Early-season team ratings are heavily shrunk toward the league mean
The Tank01 fallback uses `weight = clamp(6 / (games + 6), 0.18, 0.60)`. Examples before clipping:
- 6 games: 50% league-mean shrinkage
- 10 games: 37.5%
- 20 games: 23.1%

This substantially compresses team-strength differences early in the season. The code has no documented, out-of-sample evidence that these shrinkage weights are optimal.

**Required correction:** estimate the prior and shrinkage schedule using historical season-by-season walk-forward testing. Do not hand-tune weights to create favorites or more picks.

### 3. The spread candidate's home/away win-probability metadata is reversed
In `_candidate()`, `_logistic_cdf(0, margin_mean, scale)` is the probability that the home margin is at or below zero (away win), but it is assigned to `home_win`. The forecast payload later computes home win probability with the correct sign. This inconsistency can mislead consumers of candidate-level diagnostics.

**Required correction:** fix the label/sign and add a regression test before exposing candidate probabilities.

### 4. The official betting-pick gate is permanently closed
`quant_game_engine.project_matchup()` sets `probability_calibration_status = "provisional_not_validated"`. `game_model._qualified()` accepts a row only when that field equals `"validated"`. No calibration-validation/promotion workflow was found in the game model. Therefore the current implementation cannot qualify any sides or totals, regardless of the apparent edge. This explains why the board repeatedly returns zero official picks.

**Required correction:** build a reproducible walk-forward validation process with a predeclared acceptance rule. Only a passing validation run may promote a model version to validated. Until then, show directional model leans separately from wager recommendations; do not silently turn provisional probabilities into bets.

### 5. The current historical ledger cannot validate the full model
- `nba_game_predictions` stores only rows that pass `_qualified()`; because the gate above is closed, it does not accumulate a representative sample of all forecasts.
- `nba_market_snapshots` stores timestamped DraftKings quotes, but quote snapshots alone are not model forecasts.
- Daily team metric snapshots exist, but a valid replay must prove each rating snapshot was captured before the game and reconstruct the exact information available at prediction time.
- No existing complete game-level, timestamped forecast archive was found in the inspected code.

**Required correction:** persist every pregame forecast (not only selected bets), with immutable forecast timestamp, model version, rating snapshot ID, injury snapshot ID, schedule context, sportsbook line/price snapshot, projected scores, spread/total probabilities, and eventual final score. Exclude any record whose inputs were captured after tipoff.

### 6. Injury and player-impact handling is incomplete for game sides
The game model's team adjustment assigns zero points to listed OUT players on the rationale that their absence may already be reflected in season averages. Questionable/doubtful/day-to-day players are marked unpriced and block candidates unless EPM is available. Dunks & Threes EPM is disabled unless explicit provider betting-use authorization is configured. This is a conservative block, but it also means the model has no independently estimated player availability impact.

**Required correction:** retain the no-guessing block, and only add player impact from a licensed/authorized source or a validated historical on/off method. Record the exact injury report timestamp used.

## What must be measured before calling the model confident

Run a strictly chronological walk-forward evaluation, with no future data leakage, against:
1. A home-team baseline and a league-average scoring baseline.
2. Closing DraftKings spread and total.
3. The current model, unchanged.
4. Any proposed corrected model, evaluated only on dates after its training window.

Report:
- Game count and date range, with pregame timestamp coverage.
- Home/away win Brier score and log loss.
- Spread cover Brier score, log loss, accuracy, and ROI at fixed 1-unit stakes.
- Total over/under Brier score, log loss, accuracy, and ROI.
- Mean absolute error for home score, away score, margin, and total.
- Closing-line value, separated from realized ROI.
- Calibration by probability bucket, sample size, confidence interval, and market.
- Results by season phase and data-quality tier.

Use a chronological train / calibration / untouched test split. Never calibrate and report performance on the same games. Compare against market-implied no-vig probabilities. Include all games with valid pregame data, not only winners or selected bets.

## Release policy

- A model lean (favorite/underdog, over/under) may be displayed for every game with valid inputs.
- A confidence percentage must be explicitly labelled **uncalibrated** until the out-of-sample calibration gate passes.
- A wager is actionable only after calibration, edge, EV, data freshness, and injury checks all pass.
- If no wager qualifies, the board must say **No qualified bets** while still showing the model's directional lean and the reason it was not promoted.
- Do not force a 60% win rate, alter thresholds after seeing test results, or change the formula to match market favorites.

## Files inspected

- `src/api/game_model.py`
- `src/api/quant_game_engine.py`
- `src/api/advanced_metrics.py`
- `src/api/index.py`
- `src/api/prediction_ledger.py`
- `tests/test_nba_settlement.py`

## Validation pipeline implementation (2026-11-10)

The audit branch now includes:
- Immutable pregame forecast persistence from both the independent forecast endpoint and the scheduled game-board build.
- Daily scheduled settlement of archived forecasts against completed ESPN scoreboard results. Settlement matches verified home/away teams and tip time, so ESPN and PropLine event IDs do not need to be identical.
- A secured /api/game-validation/collect endpoint protected by CRON_SECRET.
- A /api/game-validation/report endpoint with chronological forecast accuracy, home-win Brier score/log loss, margin and total MAE, calibration buckets, last-observed pre-tip DraftKings no-vig benchmark, and an explicitly exploratory flat-stake simulation.
- One latest pregame forecast per event/model version in the report, preventing repeated refreshes from overweighting a game.
- A daily Vercel cron at 14:00 UTC to settle completed forecasts.

The daily /api/game-board cron also persists the full pregame forecast archive. DraftKings quote capture remains on its existing scheduled job. The closing benchmark is the latest complete captured pre-tip quote, not a guaranteed exact sportsbook close.

The report intentionally returns insufficient-sample results when no settled prospective forecasts exist. It does not reconstruct past forecasts from current data and does not automatically promote calibration status to validated.

## Scope of this audit branch

This commit records verified findings and the acceptance criteria. It deliberately does not change the prediction formula, data source, betting thresholds, production deployment, or existing ledger records. The engineering pipeline is implemented in this branch. Remaining proof is operational: merge/deploy it, confirm the Vercel cron and database permissions, and accumulate settled prospective forecasts. If the database lacks sufficient pregame snapshots, the report must say so rather than fabricate a backtest.
