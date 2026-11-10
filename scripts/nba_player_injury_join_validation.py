"""Validate the historical NBA injury-to-player-boxscore join before modelling.

Research only. Downloads official-source SportsDataverse parquet assets and
reports identifier coverage, report-time formats, and game-level join quality.
No live model, picks, or production data are modified.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import duckdb

OUT = Path("backtest-output/player_injury_research")
OUT.mkdir(parents=True, exist_ok=True)
SEASONS = (2025, 2026)  # ending-year keys: 2024-25, 2025-26
BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
URLS = {
    "injury": BASE + "/nba_stats_injury_report/injury_report_{season}.parquet",
    "players": BASE + "/nba_stats_player_boxscores/player_boxscores_{season}.parquet",
}


def download(url: str, path: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ColtsNBAResearch/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response, path.open("wb") as target:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)


def main() -> None:
    con = duckdb.connect()
    report = {
        "experiment": "historical_player_injury_join_validation_v1",
        "purpose": "Validate game/player identifiers and pre-tip injury report timestamps before fitting an injury-aware ATS model.",
        "seasons": {},
        "integrity": {
            "live_model_modified": False,
            "production_deployed": False,
            "outcome_data_used_to_create_injury_features": False,
        },
    }
    for season in SEASONS:
        injury_path = OUT / f"injury_report_{season}.parquet"
        player_path = OUT / f"player_boxscores_{season}.parquet"
        download(URLS["injury"].format(season=season), injury_path)
        download(URLS["players"].format(season=season), player_path)

        injury_cols = [r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(injury_path)]
        ).fetchall()]
        player_cols = [r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(player_path)]
        ).fetchall()]
        required_injury = {"report_datetime", "game_date", "game_time", "game_id",
                           "person_id", "team", "current_status"}
        required_player = {"game_id", "person_id", "team_id", "minutes",
                           "plus_minus_points", "game_date"}
        missing = {
            "injury": sorted(required_injury - set(injury_cols)),
            "player_boxscores": sorted(required_player - set(player_cols)),
        }
        if any(missing.values()):
            raise RuntimeError(f"{season} schema missing required fields: {missing}")

        injury_stats = con.execute("""
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT game_id) AS games,
                   COUNT(DISTINCT person_id) AS players,
                   COUNT(*) FILTER (WHERE person_id IS NOT NULL) AS identified_rows,
                   COUNT(*) FILTER (WHERE current_status IN ('Out','Doubtful','Questionable','Probable','Available')) AS known_status_rows,
                   COUNT(DISTINCT CAST(report_datetime AS DATE)) AS report_days,
                   MIN(report_datetime), MAX(report_datetime)
            FROM read_parquet(?)
        """, [str(injury_path)]).fetchone()
        player_stats = con.execute("""
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT game_id) AS games,
                   COUNT(DISTINCT person_id) AS players,
                   MIN(game_date), MAX(game_date)
            FROM read_parquet(?)
            WHERE CAST(game_id AS VARCHAR) IS NOT NULL
        """, [str(player_path)]).fetchone()

        join_stats = con.execute("""
            WITH i AS (
              SELECT DISTINCT CAST(game_id AS VARCHAR) game_id,
                     CAST(person_id AS VARCHAR) person_id
              FROM read_parquet(?) WHERE person_id IS NOT NULL
            ),
            p AS (
              SELECT DISTINCT CAST(game_id AS VARCHAR) game_id,
                     CAST(person_id AS VARCHAR) person_id
              FROM read_parquet(?) WHERE person_id IS NOT NULL
            )
            SELECT COUNT(*) AS injury_game_player_keys,
                   COUNT(*) FILTER (WHERE p.person_id IS NOT NULL) AS matched_keys,
                   COUNT(DISTINCT i.game_id) AS injury_games,
                   COUNT(DISTINCT i.game_id) FILTER (WHERE p.game_id IS NOT NULL) AS games_with_any_player_match
            FROM i LEFT JOIN p USING (game_id, person_id)
        """, [str(injury_path), str(player_path)]).fetchone()

        game_times = con.execute("""
            SELECT game_time, COUNT(*) AS rows
            FROM read_parquet(?)
            GROUP BY game_time ORDER BY rows DESC LIMIT 20
        """, [str(injury_path)]).fetchall()
        examples = con.execute("""
            SELECT report_datetime, game_date, game_time, game_id,
                   home_team_abbr, away_team_abbr, team, report_player_name,
                   person_id, current_status
            FROM read_parquet(?)
            ORDER BY report_datetime LIMIT 12
        """, [str(injury_path)]).fetchall()

        report["seasons"][str(season)] = {
            "assets": {
                "injury_bytes": injury_path.stat().st_size,
                "player_boxscore_bytes": player_path.stat().st_size,
            },
            "injury_schema": injury_cols,
            "player_boxscore_schema": player_cols,
            "injury_rows_games_players": {
                "rows": injury_stats[0], "games": injury_stats[1],
                "distinct_players": injury_stats[2], "identified_rows": injury_stats[3],
                "known_status_rows": injury_stats[4], "report_days": injury_stats[5],
                "first_report": str(injury_stats[6]), "last_report": str(injury_stats[7]),
            },
            "player_boxscore_rows_games_players": {
                "rows": player_stats[0], "games": player_stats[1],
                "distinct_players": player_stats[2],
                "first_game_date": str(player_stats[3]), "last_game_date": str(player_stats[4]),
            },
            "injury_to_boxscore_id_join": {
                "injury_game_player_keys": join_stats[0], "matched_keys": join_stats[1],
                "player_key_match_pct": round(100 * join_stats[1] / join_stats[0], 2) if join_stats[0] else None,
                "injury_games": join_stats[2], "games_with_any_player_match": join_stats[3],
            },
            "game_time_value_examples": [{"value": str(x[0]), "rows": x[1]} for x in game_times],
            "first_report_examples": [
                {"report_datetime": str(x[0]), "game_date": str(x[1]), "game_time": str(x[2]),
                 "game_id": str(x[3]), "home": str(x[4]), "away": str(x[5]),
                 "team": str(x[6]), "player": str(x[7]), "person_id": str(x[8]),
                 "status": str(x[9])} for x in examples
            ],
        }

    (OUT / "join_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
