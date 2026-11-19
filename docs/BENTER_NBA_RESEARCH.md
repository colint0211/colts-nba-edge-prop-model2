# Benter-style NBA prop research

## What is being implemented

This branch is an isolated research implementation. It does not change production
picks, the live UI, or the production Neon ledger.

Bill Benter's 1994 method has two distinct statistical stages:

1. **Fundamental handicapping model.** Estimate the probability of the outcome
   from sport-specific information. In this adaptation, the first-stage model
   is a regularized logistic regression trained on pregame player, role, minutes,
   matchup and game-context features.
2. **Market-information combination.** Generate first-stage probabilities
   out-of-sample, then fit a second logistic model using the log-odds of the
   fundamental probability and the no-vig DraftKings probability. For a binary
   Over/Under outcome this is the two-class equivalent of Benter's
   log-probability combination.

The second stage is not a fixed weighted average. Its coefficients are estimated
from prior settled observations only.

## Current code

- `src/api/benter_model.py`: odds conversion, no-vig normalization, expected
  value, Kelly calculation, logistic fitting, Benter second-stage combination,
  and date-grouped expanding-window walk-forward predictions.
- `src/api/benter_features.py`: strict feature contract for future Points
  snapshots. It rejects missing, implausible, unverified, or post-tipoff data.
- `src/api/benter_research.py`: read-only pilot using the existing settled
  Points ledger. It is available only on this research branch's protected
  Vercel preview.
- `tests/test_benter_model.py`: mathematical, feature-validation and
  chronological-leakage tests.

## First-stage Points feature contract

Features are frozen before tipoff and stored in this exact order:

1. Projected minutes
2. Points per minute over last 5 games
3. Points per minute over last 10 games
4. Season points per minute
5. Usage rate
6. Usage-rate change
7. Team pace
8. Opponent pace
9. Opponent defensive rating
10. Opponent points allowed to the player's position, league-standardized
11. Usage vacated by unavailable teammates
12. Rest days
13. Home indicator
14. Back-to-back indicator
15. Independent expected-points projection minus the exact sportsbook line

All inputs must have a verified source and a pre-tipoff timestamp. Missing data
is not replaced with a guessed value. The model learns coefficients from the
chronological training sample; the listed variables are not hand-assigned
weights.

## Walk-forward rules

- Group all games on the same slate date together.
- Train only on dates strictly earlier than the prediction date.
- Create out-of-sample first-stage probabilities for historical rows.
- Train the market combiner only on those historical out-of-sample probabilities.
- Keep the test date completely untouched until both stages are fit.
- Exclude pushes from binary win/loss model fitting.
- Report Brier score, log loss, calibration, hit rate, ROI and price/line
  movement separately.
- Do not select thresholds on the final holdout.
- No production deployment or writes to production tables from this branch.

## Current evidence limitation

The existing settled Points ledger contains 113 win/loss rows but only three
distinct slate dates in its current stored sample: 39 rows on October 30, 70
on November 17, and 4 on November 18, 2026. It is a selected-pick ledger, not a
complete archive of every DraftKings prop. A date-grouped two-stage pilot can
therefore produce at most a four-pick final holdout from the current rows.
That sample is useful for checking the pipeline, not for claiming an edge.

A meaningful Benter evaluation needs daily archived pregame features, both
sides' DraftKings prices at the exact line, and final box-score settlement for
a much larger chronological sample. This is the next data-collection milestone.
