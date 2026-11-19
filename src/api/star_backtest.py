"""Isolated, authenticated, read-only stars-first NBA points backtest.

Uses PropLine Hobby player history (DraftKings main lines and settled results)
joined to PropLine's official NBA game logs. It never writes to production data.
"""
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException, Request as FastRequest

router = APIRouter()
BASE = "https://api.prop-line.com/v1"
SPORT = "basketball_nba"
SEASON = "2026-27"
START_DATE = "2026-10-21"
BOOK = "draftkings"
MARKET = "player_points"
PLAYERS = [
    "Nikola Jokic", "Shai Gilgeous-Alexander", "Luka Doncic",
    "Giannis Antetokounmpo", "Jayson Tatum", "Anthony Edwards",
    "Devin Booker", "Donovan Mitchell", "Tyrese Maxey", "Stephen Curry",
    "Jalen Brunson", "Kevin Durant", "LeBron James", "Victor Wembanyama",
    "Trae Young", "Cade Cunningham", "Paolo Banchero", "Jaylen Brown",
    "De'Aaron Fox", "LaMelo Ball"
]
WEIGHT_CANDIDATES = [
    (0.50, 0.30, 0.20), (0.40, 0.40, 0.20),
    (0.30, 0.40, 0.30), (0.60, 0.25, 0.15),
    (0.35, 0.35, 0.30)
]


def _api_get(path, params):
    key = os.getenv("PROPLINE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("PROPLINE_API_KEY is not configured for this preview.")
    query = dict(params)
    url = BASE + path + "?" + urlencode(query)
    req = Request(url, headers={
        "X-API-Key": key,
        "Accept": "application/json",
        "User-Agent": "ColtsNBAEdge-Research/1.0"
    })
    try:
        with urlopen(req, timeout=14) as response:
            import json
            return json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError("PropLine HTTP " + str(exc.code) + ": " + body[:300])
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("PropLine connection failed: " + str(exc)[:180])


def _minutes(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        raw = str(value).strip()
        if ":" in raw:
            bits = raw.split(":")
            try:
                result = float(bits[0]) + float(bits[1]) / 60.0
            except (ValueError, IndexError):
                return None
        else:
            try:
                result = float(raw)
            except ValueError:
                return None
    return result if math.isfinite(result) and 0 < result <= 60 else None


def _american_decimal(price):
    p = float(price)
    if p == 0:
        raise ValueError("zero American odds")
    return 1.0 + (p / 100.0 if p > 0 else 100.0 / abs(p))


def _implied(price):
    p = float(price)
    return 100.0 / (p + 100.0) if p > 0 else abs(p) / (abs(p) + 100.0)


def _normal_cdf(x, mean, sd):
    return 0.5 * (1.0 + math.erf((x - mean) / (sd * math.sqrt(2.0))))


def _player_payload(name):
    segment = quote(name, safe="")
    history = _api_get(
        "/sports/" + SPORT + "/players/" + segment + "/history",
        {"market": MARKET, "bookmaker": BOOK, "main_line_only": "true", "limit": 100}
    )
    games = _api_get(
        "/sports/" + SPORT + "/players/" + segment + "/games",
        {"season": SEASON, "season_type": "regular", "limit": 100}
    )
    return name, history, games


def _prepare_player(name, history, games):
    game_rows = games.get("games") or []
    by_event = {}
    by_date = {}
    for game in game_rows:
        stats = game.get("stats") or {}
        try:
            pts = float(stats.get("points"))
        except (TypeError, ValueError):
            continue
        date = str(game.get("commence_time") or "")
        if not date:
            continue
        row = {
            "event_id": str(game.get("event_id")),
            "date": date,
            "points": pts,
            "minutes": _minutes(stats.get("minutes", stats.get("min")))
        }
        by_event[row["event_id"]] = row
        by_date[date[:10]] = row

    entries = history.get("entries") or []
    rows = []
    for entry in entries:
        if entry.get("bookmaker") != BOOK or entry.get("is_main_line") is False:
            continue
        if entry.get("line_moved_in_play") is True:
            continue
        event_id = str(entry.get("event_id"))
        date = str(entry.get("commence_time") or "")
        game = by_event.get(event_id) or by_date.get(date[:10])
        if not game:
            continue
        date = date or game["date"]
        if not date or date[:10] < START_DATE:
            continue
        try:
            line = float(entry.get("line"))
        except (TypeError, ValueError):
            continue
        try:
            archived_actual = float(entry.get("actual_value"))
        except (TypeError, ValueError):
            archived_actual = None
        actual = game["points"]
        if archived_actual is not None and abs(archived_actual - actual) > 0.01:
            continue
        result = "PUSH" if actual == line else ("OVER" if actual > line else "UNDER")
        rows.append({
            "player": name, "event_id": event_id, "date": date,
            "line": line,
            "actual": actual, "result": result, "minutes": game["minutes"],
            "points": actual
        })

    all_games = sorted(
        [g for g in by_event.values() if g["date"] and g["date"][:10] >= START_DATE],
        key=lambda g: (g["date"], g["event_id"])
    )
    for row in rows:
        prior = [g for g in all_games if g["date"] < row["date"]]
        if len(prior) < 5:
            continue
        row["prior_games"] = prior[-20:]
    return [r for r in rows if "prior_games" in r], len(entries), len(by_event)

def _predict(row, weights):
    prior = row["prior_games"]
    last5 = prior[-5:]
    last10 = prior[-10:]
    mean5 = sum(g["points"] for g in last5) / len(last5)
    mean10 = sum(g["points"] for g in last10) / len(last10)
    season_mean = sum(g["points"] for g in prior) / len(prior)
    mean = weights[0] * mean5 + weights[1] * mean10 + weights[2] * season_mean
    residuals = [g["points"] - mean for g in last10]
    variance = sum(x * x for x in residuals) / max(1, len(residuals) - 1)
    sd = max(4.5, min(13.5, math.sqrt(variance)))
    p_over = max(0.001, min(0.999, 1.0 - _normal_cdf(row["line"], mean, sd)))
    return mean, sd, p_over


def _score(rows, weights):
    scored = []
    for row in rows:
        mean, sd, p_over = _predict(row, weights)
        if row["result"] == "PUSH":
            continue
        scored.append({
            **row, "projection": mean, "sd": sd, "p_over": p_over,
            "target_over": 1.0 if row["result"] == "OVER" else 0.0
        })
    return scored


def _brier(rows):
    if not rows:
        return None
    return sum((r["p_over"] - r["target_over"]) ** 2 for r in rows) / len(rows)


def _summarize(rows, threshold):
    picks = []
    wins = 0
    for r in rows:
        if r["p_over"] >= threshold:
            side = "OVER"
            confidence = r["p_over"]
        elif r["p_over"] <= 1.0 - threshold:
            side = "UNDER"
            confidence = 1.0 - r["p_over"]
        else:
            continue
        won = r["result"] == side
        wins += int(won)
        picks.append({
            "player": r["player"], "date": r["date"][:10],
            "side": side, "line": r["line"],
            "projection": round(r["projection"], 2),
            "actual": r["actual"], "win": won,
            "model_probability": round(confidence, 4)
        })
    n = len(picks)
    return {
        "threshold": threshold, "bets": n, "wins": wins, "losses": n - wins,
        "win_rate_pct": round(100.0 * wins / n, 2) if n else None,
        "over_bets": sum(1 for p in picks if p["side"] == "OVER"),
        "under_bets": sum(1 for p in picks if p["side"] == "UNDER"),
        "picks": picks
    }


@router.get("/api/backtest/stars/points")
def stars_points_backtest(request: FastRequest):
    expected = os.getenv("STAR_BACKTEST_TOKEN", "")
    supplied = request.headers.get("x-backtest-token", "") or request.query_params.get("token", "")
    if not expected or not supplied or not __import__("hmac").compare_digest(expected, supplied):
        raise HTTPException(status_code=404, detail="Not found")

    fetched = []
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_player_payload, name): name for name in PLAYERS}
        for future in as_completed(futures):
            name = futures[future]
            try:
                fetched.append(future.result())
            except Exception as exc:
                errors.append({"player": name, "error": str(exc)[:250]})

    all_rows = []
    coverage = []
    for name, history, games in fetched:
        rows, history_count, game_count = _prepare_player(name, history, games)
        all_rows.extend(rows)
        hsample = (history.get("entries") or [])[:1]
        gsample = (games.get("games") or [])[:1]
        gfirst = gsample[0] if gsample else {}
        gstats = gfirst.get("stats") if isinstance(gfirst, dict) else None
        coverage.append({
            "player": name, "resolved_history_entries": history_count,
            "season_game_logs": game_count, "joined_eligible_props": len(rows),
            "history_sample": ({k: hsample[0].get(k) for k in ("event_id", "commence_time", "bookmaker", "line", "over_price", "under_price", "actual_value", "over_result", "under_result", "is_main_line", "redacted")} if hsample else None),
            "game_sample": ({"event_id": gfirst.get("event_id"), "commence_time": gfirst.get("commence_time"),
                             "stats_type": type(gstats).__name__,
                             "stats_keys": list(gstats.keys())[:15] if isinstance(gstats, dict) else None,
                             "stats_first": gstats[:3] if isinstance(gstats, list) else None, "game_dates": [{"event_id": x.get("event_id"), "date": x.get("commence_time"), "points": (x.get("stats") or {}).get("points")} for x in (games.get("games") or [])[:8]]} if gsample else None)
        })

    # Split by date, not by row, so every same-day game is held out together.
    dates = sorted(set(r["date"][:10] for r in all_rows))
    if len(dates) < 5:
        return {
            "status": "insufficient_data", "season": SEASON,
            "date_range": [dates[0], dates[-1]] if dates else None,
            "eligible_props": len(all_rows), "distinct_dates": len(dates),
            "coverage": coverage, "source_errors": errors,
            "reason": "Not enough joined DraftKings main-line results and pregame game logs to form a chronological train/test split."
        }
    split_index = max(3, min(len(dates) - 1, int(len(dates) * 0.65)))
    cutoff = dates[split_index]
    train_rows = [r for r in all_rows if r["date"][:10] < cutoff]
    test_rows = [r for r in all_rows if r["date"][:10] >= cutoff]

    train_scored = []
    for weights in WEIGHT_CANDIDATES:
        scored = _score(train_rows, weights)
        train_scored.append((_brier(scored), weights, scored))
    train_scored.sort(key=lambda x: x[0] if x[0] is not None else 999)
    best_brier, best_weights, best_train = train_scored[0]
    test_scored = _score(test_rows, best_weights)

    # Tune a confidence cutoff on training dates only, then freeze it for holdout.
    threshold_candidates = [0.50, 0.52, 0.55, 0.58, 0.60]
    train_threshold_results = []
    for threshold in threshold_candidates:
        summary = _summarize(best_train, threshold)
        if summary["bets"] >= 25:
            train_threshold_results.append(summary)
    if train_threshold_results:
        train_threshold_results.sort(
            key=lambda x: (x["win_rate_pct"], x["bets"]), reverse=True
        )
        selected_threshold = train_threshold_results[0]["threshold"]
    else:
        selected_threshold = 0.50

    holdout = _summarize(test_scored, selected_threshold)
    holdout_directional = _summarize(test_scored, 0.50)
    train_selection = _summarize(best_train, selected_threshold)
    return {
        "status": "complete",
        "season": SEASON,
        "market": "NBA player points",
        "sportsbook": "DraftKings",
        "date_range": {"first": dates[0], "last": dates[-1]},
        "train_test": {"train_through": dates[split_index - 1],
                       "holdout_from": cutoff, "train_props": len(train_rows),
                       "holdout_props": len(test_rows)},
        "star_cohort": PLAYERS,
        "coverage": coverage,
        "source_errors": errors,
        "method": {
            "prediction": "Pregame points-only blend of prior L5, L10 and season-to-date game means; residual SD from prior L10, clipped 4.5–13.5 points; normal CDF for line probability.",
            "weight_selection": "Five fixed L5/L10/season scoring-rate blends compared on training Brier score only; lowest-Brier blend frozen before holdout.",
            "selection": "Confidence cutoff selected from a fixed grid on training dates only (minimum 25 training bets), then frozen for holdout; one side per player-game.",
            "leakage_controls": "Game logs strictly before tip; same-day games held together; current-season regular season only; main line only; in-play re-priced lines excluded; player history actuals cross-checked against box scores."
        },
        "selected_weights_l5_l10_season": list(best_weights),
        "selected_confidence_threshold": selected_threshold,
        "train_brier": round(best_brier, 6) if best_brier is not None else None,
        "train_selected_bets": train_selection,
        "holdout": holdout,
        "holdout_directional_at_50pct": holdout_directional,
        "holdout_probability_brier": round(_brier(test_scored), 6) if test_scored else None,
        "roi": None,
        "roi_note": "Unavailable because PropLine Free-tier redacts both historical side prices.",
        "limitations": [
            "The fixed cohort is a predeclared star list, not a claim that these are the league's top 20 by current usage.",
            "The model uses historical points only for this baseline; it does not yet include minutes, archived pregame injury, lineup, opponent, pace or usage features.",
            "Only DraftKings main lines are evaluated; this is not a test of alternate lines or FanDuel.",
            "Free-tier responses redact DraftKings prices and settled-result fields; hit rate is calculated from the line and independently returned game-log points, but ROI and price-based EV cannot be calculated.",
            "A 30-day Hobby event-history window may constrain available odds history; report coverage is shown per player."
        ]
    }
