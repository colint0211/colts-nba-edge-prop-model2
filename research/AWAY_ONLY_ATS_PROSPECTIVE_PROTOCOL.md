# Away-only ATS prospective validation protocol

**Status:** Research-only. Not approved for live betting or production deployment.

## Frozen candidate

- Model: the existing ridge model in `scripts/nba_market_movement_ats_holdout.py`.
- Inputs: opening-to-latest home spread movement, home moneyline implied-probability movement, and total movement.
- Calibration: chronological validation residual standard deviation, using the existing script's method.
- Selection threshold: 55% calibrated probability.
- Selection rule: record an AWAY ATS pick only when calibrated `p(home cover) <= 0.45`.
- All HOME picks and all games not meeting the rule are passes.
- Price assumption for historical comparison: flat -110. For prospective grading, record the actual available away price and separately report flat -110 for comparability.
- No changing features, model, threshold, side filter, odds source, or grading rules during the validation window.

## Prospective record rules

1. Capture each eligible game's opening and latest available market values before scheduled tipoff.
2. Save the prediction, raw input values, capture timestamp, game ID, scheduled start, model version/commit, and available away price before tipoff.
3. Never overwrite or delete a saved pick. Corrections must be append-only, with the reason and timestamp recorded.
4. Grade only after the game is final, against the captured away spread and final score. Pushes are excluded from win percentage and reported separately.
5. Report every eligible game, including passes, so selection coverage can be audited.
6. Report record, win rate, pushes, actual-price units, flat -110 units, ROI, sample size, and a 95% Wilson confidence interval.
7. Publish weekly results and preserve the complete game-level ledger.
8. Do not tune the model from prospective outcomes. Any future model revision starts a separately named experiment and a new prospective ledger.

## Minimum evidence gates

- First checkpoint: 50 qualifying away picks; descriptive only.
- Second checkpoint: 100 qualifying away picks; still not proof of a durable edge.
- Stronger validation: at least 200 qualifying picks across a complete, untouched season or a later season, with positive net units at actual recorded prices and no material calibration failure.
- Compare against a simple market baseline and report the results whether positive or negative.
- Do not call the strategy validated merely because it clears 60% on a small sample.

## Data dependency

Prospective capture requires an odds feed that exposes pre-tipoff opening and latest/closing lines with timestamps. DunkAPI documents:
- `get_schedule` (2 credits) for upcoming games and median current lines.
- `get_odds` (5 credits per game or 10 credits for a date) for per-book opening/closing spreads, moneylines, totals, and capture timestamps.
- `get_line_movement` (10 credits per game) for timestamped sportsbook snapshots.

The GitHub Actions secret `DUNKAPI_API_KEY` must be configured before an automated collector can run. Never put the key in source code, workflow logs, or committed files.

## Integrity boundary

This protocol does not change the live NBA model, deploy anything to Vercel, or authorize live wagering. It freezes the research candidate so future results can falsify it fairly.
