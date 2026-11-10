# NBA ATS ten-variable feature study

**Status:** Experimental research only. This work does not alter the frozen away-only ATS candidate, its thresholds, or its prospective ledger.

## What is added

1. A ten-variable feature specification.
2. One-feature-at-a-time experiments plus a ten-feature combined experiment.
3. Full-model ablation: remove one variable at a time and measure the untouched-test change.
4. Point-in-time timestamp enforcement to reject future information.
5. Chronological 70% train / 15% validation / 15% untouched-test split.
6. Accuracy, Brier score, log loss, and a descriptive away-only ATS record/flat -110 unit calculation.

## Ten feature definitions

| Feature column | Definition |
|---|---|
| `spread_move_toward_home` | Opening home spread minus the captured pre-bet home spread; positive means movement toward home |
| `home_win_probability_move` | Captured pre-bet home implied probability minus opening implied probability, no-vig consistently |
| `total_move` | Captured pre-bet total minus opening total |
| `elo_diff_home` | Pre-game home Elo minus away Elo |
| `rolling_net_rating_diff` | Home minus away rolling net rating, computed only from games completed before prediction |
| `rest_days_diff` | Home rest days minus away rest days |
| `schedule_burden_diff` | Predefined recent schedule-load measure for home minus away |
| `injury_availability_diff` | Home minus away availability impact, based only on timestamped pre-bet reports and a documented player-impact method |
| `rolling_off_def_rating_diff` | A predeclared home/away efficiency differential; document whether this is net efficiency or separate offense/defense components before data collection |
| `travel_tz_diff` | Pre-game travel distance/time-zone burden difference, computed from known prior venue and current venue |

Do not silently change definitions after looking at test results. Any revised definition is a new experiment/version.

## Input CSV contract

The script `scripts/nba_ats_feature_study.py` accepts one row per game with these columns:

- `date_et`, `game_id`
- `prediction_timestamp_et`: when the simulated bet decision is made
- `feature_available_at_et`: latest source timestamp used to construct the feature vector
- `home_score`, `away_score`, `spread_home`, `away_odds_american`
- All ten feature columns listed above

Use ISO-8601 timestamps with timezone offsets. A feature row whose availability timestamp is later than its prediction timestamp is rejected as leakage. The current timestamp check is deliberately conservative at row level; production ingestion should preserve source-level timestamps for each feature, not just one aggregate timestamp.

The target is whether the home team covers the captured home spread. A push is not a win or loss. The away-only diagnostic selects an away wager only when predicted home-cover probability is 45% or lower. It assumes -110 for comparability; do not treat this as actual-price ROI. The provided odds column is retained in the input contract for future actual-price accounting, but the initial script does not yet use it to calculate actual-price units.

## Testing protocol

- Sort strictly by game date and split once: first 70% train, next 15% validation, final 15% untouched test.
- Fit scaling and model parameters on training data only.
- Compare intercept-only baseline, each of the ten individual features, and all ten together.
- Run ablation by removing one variable from the ten-feature model.
- Prefer lower Brier score and log loss on untouched test; accuracy and ATS units are secondary diagnostics.
- Never select features or thresholds based on untouched-test performance. If you use validation to choose a model, preserve the final test as untouched.
- Compare against the frozen market-movement candidate separately using the exact same games, timestamps, lines, and grading rules.
- Publish sample size and confidence intervals with any win-rate claims. A small sample is descriptive, not proof.

## Leakage checklist

Reject or rebuild any feature that uses:
- Closing lines when the intended prediction time precedes market close.
- Injury designations or lineup confirmations published after prediction time.
- Actual minutes, starting lineups learned after lock, or post-game player/team stats.
- Rolling averages that include the current game or later games.
- Full-season ratings computed using games after the prediction date.
- Elo ratings updated with the game being predicted.
- Retrospectively revised injury/availability records without original publication timestamps.
- Travel or schedule information not yet known at prediction time.

## Known data gap

The existing daily market collector stores a daily quote, but the feature study needs a complete point-in-time snapshot at the exact intended prediction time, plus historical snapshots for backtesting. It also needs timestamped historical injury reports and pre-game team/player statistics. Do not manufacture missing values or substitute post-game data. Until those inputs exist, the script is a runnable research harness, not a completed historical test.

## Run

```bash
python scripts/nba_ats_feature_study.py path/to/point_in_time_ats_features.csv
```

Output: `backtest-output/ats_feature_study/results.json`.

## Frozen-model boundary

The frozen candidate remains the three-feature ridge market-movement model and its existing away-only selection rule. This feature study is a separate experimental branch and must not feed production picks or overwrite the prospective ledger.


## Mandatory historical-data quality gate

Before preparing the point-in-time feature CSV or fitting any experiment, run `scripts/audit_nba_dataset.py` against every historical source file. Preserve the original inputs. Review the aggregate JSON, per-file issue logs, and correction logs. Do not train on unresolved duplicate player/game keys, ambiguous player identity mappings, malformed dates, or unverified post-tipoff features. Do not automatically drop outliers or duplicate rows; document an explicit adjudication decision and keep a reproducible exclusion list if a row must be excluded. The audit tool retains all records and writes a normalized copy plus complete issue/correction reports. A completed audit is a prerequisite for any claim that the feature-study dataset is clean.
