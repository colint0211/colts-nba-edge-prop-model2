# Historical Prediction Error Diagnostics

Research-only diagnostics for both the player-prop and game ATS models. These
scripts read settled historical forecasts, write reports, and never update a
model, prediction ledger, production app, or frozen ATS candidate.

## Player props

Export settled rows from \`nba_predictions\` with at least:
\`player, team, market, line, projection, actual_value, outcome, snapshot\`.
Also export \`side, model_probability, blended_probability, created_at,
settled_at\` when available. Include \`opponent\` as a separate column if
present in the immutable snapshot. A minimal PostgreSQL export query:

\`\`\`sql
SELECT id, player, team, market, line, side, model_probability,
       blended_probability, projection, actual_value, outcome,
       created_at, settled_at, snapshot::text AS snapshot
FROM nba_predictions
WHERE settled_at IS NOT NULL
ORDER BY created_at, id;
\`\`\`

Run:
\`\`\`bash
python scripts/player_prop_error_diagnostics.py settled_props.csv
\`\`\`

Outputs overall and splits by player, team, opponent, market, projection band,
and betting-line band, plus a row-level residual ledger. Signed projection
error is projection minus actual: positive means overprojection, negative
means underprojection. MAE/RMSE measure magnitude. Hit-rate confidence
intervals are Wilson intervals. The mean-bias interval is a descriptive
normal approximation and should be treated cautiously for skewed/count data.

## ATS

Prepare one row per game with:
\`game_id, date_et, home_team, away_team, home_score, away_score,
spread_home, p_home_cover\`. Include \`projected_home_margin\` and
\`projected_total\` if available. Join the walk-forward predictions to the
original point-in-time game/market dataset by immutable game_id. Do not join
on team names/date alone if a stable game ID exists.

Run:
\`\`\`bash
python scripts/ats_error_diagnostics.py enriched_ats_games.csv
\`\`\`

Reports home/away team splits, spread magnitude, favorite/underdog, predicted
home-cover probability, probability Brier score, and (when forecasts exist)
margin/total bias, MAE and RMSE. For ATS result, home margin + home spread
greater than zero is a home cover; less than zero is an away cover.

## Interpretation / acceptance gate

- Default subgroup flag threshold is 30 independent observations. This is a
  screening threshold, not proof of power; small groups are reported but marked.
- Look at bias and its interval, not just win rate. A player can have high MAE
  without directional bias; that suggests variance/noise rather than a
  correctable systematic offset.
- A large number of subgroup comparisons creates false discoveries. Treat
  flags as hypotheses; use shrinkage or hierarchical modelling before
  player-specific corrections.
- Do not optimize thresholds on the same period used to report performance.
  Any proposed change must be frozen in advance and compared against the
  unchanged baseline on a later chronological holdout, reporting calibration,
  MAE/RMSE, ATS/prop hit rate, ROI at actual odds, and sample size.
- A historical miss breakdown is not a backtest of a correction. No formula
  change is accepted without untouched out-of-sample evidence.
- The prop ledger currently contains player/team/market/line/projection and
  settled outcome fields. Opponent and point-in-time metadata availability
  must be audited in actual snapshots; missing fields remain UNKNOWN and are
  excluded from those splits rather than inferred.
