"""Leakage-safe historical team-score backtest using public NBA Data PBP Parquet.

This is a predictive validation harness, not a sportsbook ROI backtest: the source
contains no historical DraftKings closing spreads/totals. Every forecast uses
only games completed before the forecast date. Same-day games are scored before
any same-day results are added to team form.
"""
import json
import math
import os
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.request import Request, urlopen

SOURCE = "https://github.com/ramirobentes/nba_data"
BASE = "https://github.com/ramirobentes/nba_data/releases/download/pbp"
SEASONS = (2025, 2026, 2027)
PRIOR_GAMES = 10
MIN_PRIOR_GAMES = 3
HOME_COURT_POINTS = 2.0
MARGIN_SD = 12.5
TOTAL_SD = 18.0


def _connect():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def _download(url, path, max_bytes=80_000_000):
    req = Request(url, headers={"User-Agent": "ColtsNBAEdge/2.0", "Accept": "application/octet-stream"})
    size = 0
    with urlopen(req, timeout=45) as response, open(path, "wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeError("Historical PBP file exceeds the 80 MB safety limit.")
            output.write(chunk)
    return size


def _cdf(x, mean, sd):
    return 0.5 * (1.0 + math.erf((x - mean) / (sd * math.sqrt(2.0))))


def _team_form(history):
    if not history:
        return None
    rows = list(history)[-PRIOR_GAMES:]
    poss_for = sum(x[0] for x in rows)
    pts_for = sum(x[1] for x in rows)
    poss_against = sum(x[2] for x in rows)
    pts_against = sum(x[3] for x in rows)
    if min(poss_for, poss_against) <= 0:
        return None
    return {
        "off": pts_for / poss_for,
        "def": pts_against / poss_against,
        "pace": (poss_for + poss_against) / (2.0 * len(rows)),
        "games": len(rows),
    }


def _score_game(home, away, forms, league):
    h = _team_form(forms[home])
    a = _team_form(forms[away])
    if not h or not a or min(h["games"], a["games"]) < MIN_PRIOR_GAMES:
        return None
    pace = (h["pace"] + a["pace"]) / 2.0
    home_ppp = (h["off"] + a["def"]) / 2.0
    away_ppp = (a["off"] + h["def"]) / 2.0
    # Small regression to league scoring environment reduces early noisy form.
    home_ppp = 0.85 * home_ppp + 0.15 * league["ppp"]
    away_ppp = 0.85 * away_ppp + 0.15 * league["ppp"]
    pace = 0.85 * pace + 0.15 * league["pace"]
    home_points = home_ppp * pace + HOME_COURT_POINTS / 2.0
    away_points = away_ppp * pace - HOME_COURT_POINTS / 2.0
    return {
        "home": home_points,
        "away": away_points,
        "margin": home_points - away_points,
        "total": home_points + away_points,
        "home_games": h["games"],
        "away_games": a["games"],
    }


def _init_db():
    with _connect() as conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS nba_historical_backtests (
            id BIGSERIAL PRIMARY KEY,
            run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source TEXT NOT NULL,
            seasons INTEGER[] NOT NULL,
            status TEXT NOT NULL,
            results JSONB NOT NULL
          );
          CREATE INDEX IF NOT EXISTS nba_historical_backtests_latest
            ON nba_historical_backtests(run_at DESC);
        """)


def status():
    _init_db()
    with _connect() as conn:
        row = conn.execute("""
          SELECT run_at,status,results FROM nba_historical_backtests
          ORDER BY run_at DESC LIMIT 1
        """).fetchone()
    return {
        "source": SOURCE,
        "historical_seasons": list(SEASONS),
        "latest_run": (
            {"run_at": row[0].isoformat(), "status": row[1], "results": row[2]}
            if row else None
        ),
        "scope": "walk-forward team score forecasting; not historical betting ROI or closing-line-value validation",
        "player_lineup_metrics_included": False,
        "model_influence": "disabled_until_out_of_sample_results_reviewed",
    }


def run(persist=True):
    import duckdb
    if persist:
        _init_db()
    all_games = []
    files = {}
    with tempfile.TemporaryDirectory(prefix="nba-pbp-backtest-") as folder:
        db = duckdb.connect(database=":memory:")
        try:
            for season in SEASONS:
                filename = f"pbp_regular_{season}.parquet"
                path = os.path.join(folder, filename)
                size = _download(f"{BASE}/{filename}", path)
                files[str(season)] = {"file": filename, "download_bytes": size}
                escaped = path.replace("'", "''")
                rows = db.execute(f"""
                  SELECT game_date, game_id, team_home, team_away,
                         MAX(hs) AS home_score, MAX(vs) AS away_score,
                         MAX(poss_home) AS home_possessions,
                         MAX(poss_away) AS away_possessions
                  FROM read_parquet('{escaped}')
                  WHERE season_type='regular'
                  GROUP BY game_date,game_id,team_home,team_away
                  HAVING MAX(hs)>0 AND MAX(vs)>0
                     AND MAX(poss_home)>0 AND MAX(poss_away)>0
                  ORDER BY game_date,game_id
                """).fetchall()
                for row in rows:
                    all_games.append({
                        "date": str(row[0]), "season": season, "game_id": str(row[1]),
                        "home": str(row[2]), "away": str(row[3]),
                        "home_score": float(row[4]), "away_score": float(row[5]),
                        "home_poss": float(row[6]), "away_poss": float(row[7]),
                    })
        finally:
            db.close()

    all_games.sort(key=lambda x: (x["date"], x["game_id"]))
    forms = defaultdict(lambda: deque(maxlen=PRIOR_GAMES))
    league_games = deque(maxlen=200)
    predictions = []
    scored_dates = 0
    idx = 0
    while idx < len(all_games):
        day = all_games[idx]["date"]
        batch = []
        while idx < len(all_games) and all_games[idx]["date"] == day:
            batch.append(all_games[idx])
            idx += 1
        league = {
            "ppp": (sum(g["home_score"] + g["away_score"] for g in league_games) /
                    sum(g["home_poss"] + g["away_poss"] for g in league_games))
                   if league_games else 1.12,
            "pace": (sum(g["home_poss"] + g["away_poss"] for g in league_games) /
                     (2.0 * len(league_games))) if league_games else 99.0,
        }
        day_predictions = []
        for game in batch:
            forecast = _score_game(game["home"], game["away"], forms, league)
            if forecast:
                actual_margin = game["home_score"] - game["away_score"]
                actual_total = game["home_score"] + game["away_score"]
                win_prob = 1.0 - _cdf(0.0, forecast["margin"], MARGIN_SD)
                actual_win = 1.0 if actual_margin > 0 else 0.0
                day_predictions.append({
                    "date": day, "season": game["season"], "game_id": game["game_id"],
                    "home": game["home"], "away": game["away"],
                    "projected_home": forecast["home"], "projected_away": forecast["away"],
                    "projected_margin": forecast["margin"], "projected_total": forecast["total"],
                    "actual_home": game["home_score"], "actual_away": game["away_score"],
                    "actual_margin": actual_margin, "actual_total": actual_total,
                    "margin_abs_error": abs(forecast["margin"] - actual_margin),
                    "total_abs_error": abs(forecast["total"] - actual_total),
                    "home_win_probability": win_prob,
                    "winner_brier": (win_prob - actual_win) ** 2,
                    "margin_sd": MARGIN_SD, "total_sd": TOTAL_SD,
                    "prior_games_home": forecast["home_games"],
                    "prior_games_away": forecast["away_games"],
                })
        predictions.extend(day_predictions)
        # Update team and league state only after every game on this date was scored.
        for game in batch:
            forms[game["home"]].append((game["home_poss"], game["home_score"],
                                        game["away_poss"], game["away_score"]))
            forms[game["away"]].append((game["away_poss"], game["away_score"],
                                        game["home_poss"], game["home_score"]))
            league_games.append(game)
        if day_predictions:
            scored_dates += 1

    if not predictions:
        raise RuntimeError("No historical games had sufficient prior team history for walk-forward scoring.")

    def mean(key):
        return sum(x[key] for x in predictions) / len(predictions)

    margins = [x["actual_margin"] for x in predictions]
    totals = [x["actual_total"] for x in predictions]
    margin_bias = mean("projected_margin") - sum(margins) / len(margins)
    total_bias = mean("projected_total") - sum(totals) / len(totals)
    calibration_bins = []
    for lower in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        upper = lower + 0.1
        bucket = [x for x in predictions
                  if lower <= x["home_win_probability"] < upper
                  or (upper >= 1.0 and x["home_win_probability"] == 1.0)]
        if bucket:
            calibration_bins.append({
                "probability_range": f"{lower:.1f}-{upper:.1f}",
                "games": len(bucket),
                "mean_predicted_home_win_pct": round(
                    100.0 * sum(x["home_win_probability"] for x in bucket) / len(bucket), 2),
                "actual_home_win_pct": round(
                    100.0 * sum(x["actual_margin"] > 0 for x in bucket) / len(bucket), 2),
            })
    def summarize(rows):
        if not rows:
            return None
        n = len(rows)
        return {
            "games": n,
            "margin_mae_points": round(sum(x["margin_abs_error"] for x in rows) / n, 3),
            "total_mae_points": round(sum(x["total_abs_error"] for x in rows) / n, 3),
            "margin_bias_points": round(sum(x["projected_margin"] - x["actual_margin"] for x in rows) / n, 3),
            "total_bias_points": round(sum(x["projected_total"] - x["actual_total"] for x in rows) / n, 3),
            "winner_accuracy_pct": round(100.0 * sum(
                (x["projected_margin"] > 0) == (x["actual_margin"] > 0) for x in rows
            ) / n, 2),
            "winner_brier": round(sum(x["winner_brier"] for x in rows) / n, 5),
        }

    season_metrics = {
        str(season): summarize([x for x in predictions if x["season"] == season])
        for season in SEASONS
    }
    results = {
        "method": "Rolling last-10 team offensive/defensive points per possession and pace; only prior completed dates; same-day results held out; league shrinkage 15%; home-court +2 points.",
        "files": files,
        "games_available": len(all_games),
        "games_scored": len(predictions),
        "dates_scored": scored_dates,
        "date_range": {"first": predictions[0]["date"], "last": predictions[-1]["date"]},
        "season_metrics": season_metrics,
        "holdout_season": str(max(SEASONS)),
        "holdout_metrics": season_metrics.get(str(max(SEASONS))),
        "metrics": {
            "margin_mae_points": round(mean("margin_abs_error"), 3),
            "total_mae_points": round(mean("total_abs_error"), 3),
            "margin_bias_points": round(margin_bias, 3),
            "total_bias_points": round(total_bias, 3),
            "winner_accuracy_pct": round(100.0 * sum(
                (x["projected_margin"] > 0) == (x["actual_margin"] > 0) for x in predictions
            ) / len(predictions), 2),
            "winner_brier": round(mean("winner_brier"), 5),
            "baseline_winner_brier_50_50": 0.25,
            "winner_brier_skill_vs_50_50": round(
                1.0 - mean("winner_brier") / 0.25, 5),
            "winner_probability_calibration_bins": calibration_bins,
        },
        "limitations": [
            "No historical DraftKings spreads or totals are present in this source; ROI, spread-cover calibration, total-line calibration and closing-line value are not measured.",
            "No injury or pregame availability archive is included in this baseline; the model is team-form only.",
            "Player on/off and lineup metrics are not included in this historical baseline.",
            "Historical PBP source coverage and final-score completeness are validated by row filters, not independently audited against official NBA box scores."
        ],
        "model_influence": "disabled_until_out_of_sample_results_reviewed",
    }
    if persist:
        with _connect() as conn:
            conn.execute("""
              INSERT INTO nba_historical_backtests(source,seasons,status,results)
              VALUES(%s,%s,%s,%s::jsonb)
            """, (SOURCE, list(SEASONS), "complete", json.dumps(results, allow_nan=False)))
    else:
        with open("backtest_results.json", "w", encoding="utf-8") as output:
            json.dump({"success": True, **results}, output, indent=2, allow_nan=False)
    return {"success": True, **results}
