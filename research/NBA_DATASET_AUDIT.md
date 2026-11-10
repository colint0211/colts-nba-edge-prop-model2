Historical NBA dataset audit

Run before feature construction or backtesting:

    python scripts/audit_nba_dataset.py path/to/nba_data.csv
    python scripts/audit_nba_dataset.py path/to/data_folder --out backtest-output/data_audit
    python scripts/audit_nba_dataset.py path/to/nba_data.csv --season-start 2024-10-22 --season-end 2025-04-13

CSV uses the Python standard library. Parquet requires pandas and pyarrow.

Outputs:
- cleaned CSV: all source rows retained; whitespace and parseable date formats normalized; each row receives audit_issue_codes.
- issues CSV: every unresolved issue with row number, severity, field, observed value, and explanation.
- corrections CSV: every correction with before/after values.
- per-file JSON and aggregate NBA_DATA_AUDIT_SUMMARY.json.

Checks: missing IDs/dates; duplicate game and player-game keys; exact duplicate rows; player name/ID conflicts; malformed/out-of-season dates; invalid/impossible/non-integer statistics; robust MAD outliers; missing timezone offsets; source timestamps after tipoff; and fields whose names suggest actual/live/post-game statistics.

Safeguards: repeated game IDs are expected in player-game tables; duplicate game_id + player_id keys are flagged. Identity conflicts are never automatically remapped. Post-game box-score stats may be labels/outcomes but not pregame predictors. Outliers are flagged, never clipped. No source is modified and no rows are deleted.

Limitation: this audits files supplied to it. It does not download or certify remote datasets. The real audit report requires the historical files to be supplied or mounted in the execution environment.
