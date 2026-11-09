"""Leakage-safe NBA historical score backtest from the public nba_data PBP releases.

The model is validation-only. It does not alter current projections or picks.
Each season is evaluated independently to prevent stale cross-offseason form.
"""
import json
import os
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.request import Request, urlopen

SOURCE = "https://github.com/ramirobentes/nba_data"
SEASONS = (2025, 2026, 2027)
BASE = "https://github.com/ramirobentes/nba_data/releases/download/pbp/pbp_regular_{season}.parquet"
MARGIN_SD = 12.0


def _db():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def _initialize():
    with _db() as conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS nba_historical_backtests (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL,
            results JSONB NOT NULL
          )
        """)


def status():
    _initialize()
    with _db() as conn:
        row = conn.execute(
            "SELECT created_at,status,results FROM nba_historical_backtests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "source": SOURCE,
        "seasons_requested": list(SEASONS),
        "model_influence": "disabled_pending_walk_forward_validation",
        "latest_run": None if not row else {
            "created_at": row[0].isoformat(),
            "status": row[1],
            "results": row[2],
        },
    }


def _download(season):
    url = BASE.format(season=season)
    req = Request(url, headers={"User-Agent": "Colts-NBA-Research/1.0"})
    with urlopen(req, timeout=25) as response:
        data = response.read()
    if len(data) < 1000:
        raise RuntimeError(f"Season {season} Parquet release was unexpectedly small")
    handle = tempfile.NamedTemporaryFile(suffix=f"_{season}.parquet", delete=False)
    try:
        handle.write(data)
        return handle.name
    finally:
        handle.close()


def _games(path):
    import duckdb
    con = duckdb.connect()
    try:
        columns = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
        required = {"game_date", "game_id", "team_home", "team_away", "hs", "vs", "season_type"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError("PBP schema missing required fields: " + ", ".join(missing))
        sql = f"""
          SELECT game_date, game_id, team_home, team_away,
                 MAX(hs) AS home_score, MAX(vs) AS away_score,
                 MAX(poss_home) AS home_possessions,
                 MAX(poss_away) AS away_possessions
          FROM read_parquet('{path}')
          WHERE LOWER(season_type)='regular'
          GROUP BY game_date, game_id, team_home, team_away
          HAVING MAX(hs)>0 AND MAX(vs)>0
          ORDER BY game_date, game_id
        """
        rows = con.execute(sql).fetchall()
        return rows
    finally:
        con.close()


def _team_rates(history, league_ppp, league_pace):
    # Recent form only; every observation in history is from an earlier date.
    games = list(history)[-10:]
    if len(games) < 3:
        return None
    pf = sum(g["pf"] for g in games)
    pa = sum(g["pa"] for g in games)
    own_poss = sum(g["poss"] for g in games)
    opp_poss = sum(g["opp_poss"] for g in games)
    if own_poss <= 0 or opp_poss <= 0:
        return None
    off = pf / own_poss
    defense = pa / opp_poss
    pace = sum(g["pace"] for g in games) / len(games)
    # Modest regression to the contemporaneous league scoring/pace environment.
    off = 0.85 * off + 0.15 * league_ppp
    defense = 0.85 * defense + 0.15 * league_ppp
    pace = 0.85 * pace + 0.15 * league_pace
    return off, defense, pace


def run():
    import math
    files = []
    try:
        all_results = []
        season_summaries = []
        for season in SEASONS:
            path = _download(season)
            files.append(path)
            rows = _games(path)
            if not rows:
                raise RuntimeError(f"No regular-season games found for season {season}")
            histories = defaultdict(lambda: deque(maxlen=10))
            league_games = []
            season_predictions = []
            # First pass is date-batched: all games on a date are forecast before
            # any game from that date is added to team histories.
            by_date = defaultdict(list)
            for row in rows:
                date, gid, home, away, hs, vs, hp, ap = row
                if hp is None or ap is None or float(hp) <= 0 or float(ap) <= 0:
                    continue
                by_date[str(date)].append((str(gid), str(home), str(away), float(hs), float(vs), float(hp), float(ap)))
            for game_date in sorted(by_date):
                day_games = by_date[game_date]
                prior = [g for g in league_games[-200:]]
                if not prior:
                    league_ppp = 1.12
                    league_pace = 99.0
                else:
                    total_points = sum(g["pf"] + g["pa"] for g in prior)
                    total_poss = sum(g["poss"] + g["opp_poss"] for g in prior)
                    league_ppp = total_points / total_poss if total_poss else 1.12
                    league_pace = sum(g["pace"] for g in prior) / len(prior)
                scored = []
                for gid, home, away, hs, vs, hp, ap in day_games:
                    h = _team_rates(histories[home], league_ppp, league_pace)
                    a = _team_rates(histories[away], league_ppp, league_pace)
                    if h is None or a is None:
                        continue
                    hoff, hdef, hpace = h
                    aoff, adef, apace = a
                    pace = (hpace + apace) / 2.0
                    home_ppp = (hoff + adef) / 2.0
                    away_ppp = (aoff + hdef) / 2.0
                    home_pred = home_ppp * pace + 1.0
                    away_pred = away_ppp * pace - 1.0
                    pred_margin = home_pred - away_pred
                    pred_total = home_pred + away_pred
                    actual_margin = hs - vs
                    actual_total = hs + vs
                    home_win = 1.0 if actual_margin > 0 else 0.0
                    p_home = 1.0 / (1.0 + math.exp(-pred_margin / MARGIN_SD))
                    scored.append({
                        "game_id": gid, "date": game_date,
                        "margin_error": pred_margin - actual_margin,
                        "total_error": pred_total - actual_total,
                        "winner_correct": int((pred_margin > 0) == (actual_margin > 0)),
                        "winner_brier": (p_home - home_win) ** 2,
                        "baseline_brier": (0.5 - home_win) ** 2,
                    })
                season_predictions.extend(scored)
                # Only now update histories with that date's final results.
                for gid, home, away, hs, vs, hp, ap in day_games:
                    pace = (hp + ap) / 2.0
                    league_games.append({"pf": hs, "pa": vs, "poss": hp, "opp_poss": ap, "pace": pace})
                    histories[home].append({"pf": hs, "pa": vs, "poss": hp, "opp_poss": ap, "pace": pace})
                    histories[away].append({"pf": vs, "pa": hs, "poss": ap, "opp_poss": hp, "pace": pace})
            if not season_predictions:
                raise RuntimeError(f"Season {season}: fewer than three prior games per team; no test predictions")
            all_results.extend(season_predictions)
            season_summaries.append({
                "season_end_year": season,
                "games_in_source": len(rows),
                "games_tested": len(season_predictions),
                "date_start": min(x["date"] for x in season_predictions),
                "date_end": max(x["date"] for x in season_predictions),
                "margin_mae": round(sum(abs(x["margin_error"]) for x in season_predictions) / len(season_predictions), 3),
                "total_mae": round(sum(abs(x["total_error"]) for x in season_predictions) / len(season_predictions), 3),
                "margin_bias": round(sum(x["margin_error"] for x in season_predictions) / len(season_predictions), 3),
                "total_bias": round(sum(x["total_error"] for x in season_predictions) / len(season_predictions), 3),
                "winner_accuracy": round(sum(x["winner_correct"] for x in season_predictions) / len(season_predictions), 4),
                "winner_brier": round(sum(x["winner_brier"] for x in season_predictions) / len(season_predictions), 4),
                "50_50_brier_baseline": round(sum(x["baseline_brier"] for x in season_predictions) / len(season_predictions), 4),
            })
        n = len(all_results)
        results = {
            "source": SOURCE,
            "method": "Rolling last-10 team points-per-possession and pace, 15% regression to league environment, +2 home-court margin; all same-date games held out before updates.",
            "season_summaries": season_summaries,
            "games_tested": n,
            "margin_mae": round(sum(abs(x["margin_error"]) for x in all_results) / n, 3),
            "total_mae": round(sum(abs(x["total_error"]) for x in all_results) / n, 3),
            "margin_bias": round(sum(x["margin_error"] for x in all_results) / n, 3),
            "total_bias": round(sum(x["total_error"] for x in all_results) / n, 3),
            "winner_accuracy": round(sum(x["winner_correct"] for x in all_results) / n, 4),
            "winner_brier": round(sum(x["winner_brier"] for x in all_results) / n, 4),
            "50_50_brier_baseline": round(sum(x["baseline_brier"] for x in all_results) / n, 4),
            "limitations": [
                "No historical DraftKings closing lines: spread/total betting ROI, cover calibration and closing-line value are unavailable.",
                "Historical injury/availability archives and player/lineup impact adjustments are excluded.",
                "No claims of betting profitability are supported by this score-forecasting test.",
                "Raw source data is not redistributed; repository license status is not established."
            ],
            "model_influence": "disabled_pending_walk_forward_validation",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _initialize()
        with _db() as conn:
            conn.execute(
                "INSERT INTO nba_historical_backtests(status,results) VALUES (%s,%s)",
                ("complete", json.dumps(results)),
            )
        return {"success": True, "status": "complete", "results": results}
    except Exception as exc:
        result = {
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "model_influence": "disabled_pending_walk_forward_validation",
        }
        try:
            _initialize()
            with _db() as conn:
                conn.execute(
                    "INSERT INTO nba_historical_backtests(status,results) VALUES (%s,%s)",
                    ("failed", json.dumps(result)),
                )
        except Exception:
            pass
        raise
    finally:
        for path in files:
            try:
                os.unlink(path)
            except OSError:
                pass
