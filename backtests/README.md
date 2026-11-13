# Historical backtest workflow

This folder contains the evaluator used after each model has generated **pregame, out-of-sample** predictions from archived data. It does not treat today's odds or current-season team/player averages as historical snapshots.

## CSV contract

Required columns:
- `event_date`: game date (ISO date or timestamp)
- `market`: e.g. moneyline, spread, total, points, rebounds, assists, steals, threes
- `model_probability`: probability for the recorded selection, 0–1
- `odds_american`: price actually available at the recorded snapshot
- `outcome`: WIN, LOSS, or PUSH

Recommended columns:
- `season`, `event_start`, `prediction_created_at`, `features_as_of`
- `selection`, `line`, `closing_line`, `projection`, `actual_value`
- `market_probability` (for a separate market-baseline comparison)

## Run

```bash
python backtests/evaluate.py path/to/pregame_predictions.csv --group market,season --output backtest_report.json
```

The report includes flat-one-unit net units and ROI, win rate, Brier score, log loss, projection MAE, mean closing-line value when available, calibration bins, and rejected rows. Missing prices/outcomes are not invented. Pushes are excluded from ROI denominator and probability scores.

## Required research protocol

1. Recreate each historical prediction using only information timestamped before tipoff.
2. Fit/re-estimate parameters only on earlier dates/seasons (walk-forward); never train on the test game or later data.
3. Preserve sportsbook, timestamp, line, and price. Closing-line results are a separate diagnostic from the price actually bet.
4. Report every season and market, plus a pooled result. Include a market/no-vig baseline and ablations (base model, Four Factors, Monte Carlo / matchup additions).
5. Do not call a result profitable unless historical prices and settled outcomes are both present.

This evaluator is a scoring layer, not evidence that a backtest has already run. A valid performance report requires the historical inputs and model-specific walk-forward prediction generation.
