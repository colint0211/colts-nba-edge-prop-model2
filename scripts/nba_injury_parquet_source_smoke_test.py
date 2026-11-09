"""Smoke-test the structured SportsDataverse NBA historical injury-report Parquet assets.

The report_datetime field is the publication timestamp (US Eastern, naive).
This test only verifies coverage/schema; it does not yet claim a predictive edge.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import duckdb

OUT = Path("backtest-output/injury_data_source")
OUT.mkdir(parents=True, exist_ok=True)
SEASONS = (2025, 2026)  # ending-year labels: 2024-25 and 2025-26
BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_injury_report/injury_report_{season}.parquet"

def main() -> None:
    reports = {}
    con = duckdb.connect()
    for season in SEASONS:
        url = BASE.format(season=season)
        path = OUT / f"injury_report_{season}.parquet"
        request = urllib.request.Request(url, headers={"User-Agent": "ColtsNBAHistoricalResearch/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        table = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)]).description
        columns = [column[0] for column in table]
        required = {
            "season", "report_datetime", "game_date", "game_time", "report_matchup",
            "team", "report_player_name", "current_status", "reason", "person_id",
        }
        missing = sorted(required - set(columns))
        if missing:
            raise RuntimeError(f"{season} missing required columns: {missing}")
        row = con.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT CAST(report_datetime AS DATE)) AS report_days,
                   MIN(report_datetime) AS first_report,
                   MAX(report_datetime) AS last_report,
                   COUNT(DISTINCT game_id) AS games,
                   COUNT(*) FILTER (WHERE current_status = 'Out') AS out_rows,
                   COUNT(*) FILTER (WHERE current_status = 'Questionable') AS questionable_rows,
                   COUNT(*) FILTER (WHERE current_status = 'Doubtful') AS doubtful_rows,
                   COUNT(*) FILTER (WHERE current_status = 'Probable') AS probable_rows,
                   COUNT(*) FILTER (WHERE current_status = 'Available') AS available_rows,
                   COUNT(*) FILTER (WHERE report_player_name IS NULL) AS not_submitted_rows
            FROM read_parquet(?)
            """,
            [str(path)],
        ).fetchone()
        reports[str(season)] = {
            "url": url,
            "file_bytes": path.stat().st_size,
            "columns": columns,
            "rows": row[0],
            "report_days": row[1],
            "first_report_datetime": str(row[2]),
            "last_report_datetime": str(row[3]),
            "distinct_games": row[4],
            "status_counts": {
                "Out": row[5], "Questionable": row[6], "Doubtful": row[7],
                "Probable": row[8], "Available": row[9],
                "not_submitted": row[10],
            },
        }
        if not row[0] or not row[4]:
            raise RuntimeError(f"{season} asset returned no report rows or games")
    result = {
        "source": "SportsDataverse NBA Stats historical injury reports",
        "timestamp_semantics": "report_datetime is the publication timestamp in US Eastern, naive",
        "seasons": reports,
        "status": "schema and asset smoke test passed",
        "warning": "A separate game-level join must choose the latest report_datetime at or before each actual tipoff; later reports are forbidden to avoid look-ahead leakage.",
    }
    (OUT / "source_smoke_test.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
