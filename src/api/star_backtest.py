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
    for game in game_rows:
        stats = game.get("stats") or {}
        pts = stats.get("points")
        mins = _minutes(stats.get("minutes", stats.get("min")))
        try:
            pts = float(pts)
        except (TypeError, ValueError):
            continue
        if mins is None or not math.isfinite(pts):
            continue
        by_event[str(game.get("event_id"))] = {
            "event_id": str(game.get("event_id")),
            "date": str(game.get("commence_time") or ""),
            "points": pts, "minutes": mins
        }

    entries = history.get("entries") or []
    rows = []
    for entry in entries:
        if entry.get("bookmaker") != BOOK or entry.get("is_main_line") is False:
            continue
        if entry.get("line_moved_in_play") is True:
            continue
        event_id = str(entry.get("event_id"))
        game = by_event.get(event_id)
        if not game:
            continue
        date = str(entry.get("commence_time") or game.get("date") or "")
        if not date or date[:10] < START_DATE:
            continue
        try:
            line = float(entry.get("line"))
            over_price = float(entry.get("over_price"))
            under_price = float(entry.get("under_price"))
            actual = float(entry.get("actual_value"))
        except (TypeError, ValueError):
            continue
        # Reject a resolution/game-log mismatch rather than silently trusting a bad join.
        if abs(actual - game["points"]) > 0.01:
            continue
        if actual == line:
            result = "PUSH"
        elif actual > line:
            result = "OVER"
        else:
            result = "UNDER"
        rows.append({
            "player": name, "event_id": event_id, "date": date,
            "line": line, "over_price": over_price, "under_price": under_price,
            "actual": actual, "result": result, "minutes": game["minutes"],
            "points": game["points"]
        })

    # Only game-log rows dated before each prop are eligible as model inputs.
    all_games = sorted(
        [g for g in by_event.values() if g["date"] and g["date"][:10] >= START_DATE],
        key=lambda g: (g["date"], g["event_id"])
    )
    for row in rows:
        prior = [g for g in all_games if g["date"] < row["date"]]
        if len(prior) < 5:
            continue
        prior = prior[-20:]
        row["prior_games"] = prior
    return [r for r in rows if "prior_games" in r], len(entries), len(by_event)


def _predict(row, weights):
    prior = row["prior_games"]
    last5 = prior[-5:]
    last10 = prior[-10:]
    def ppm(items):
        total_minutes = sum(g["minutes"] for g in items)
        return sum(g["points"] for g in items) / total_minutes if total_minutes else 0.0
    rate = weights[0] * ppm(last5) + weights[1] * ppm(last10) + weights[2] * ppm(prior)
    mins5 = sum(g["minutes"] for g in last5) / len(last5)
    mins10 = sum(g["minutes"] for g in last10) / len(last10)
    expected_minutes = 0.60 * mins5 + 0.40 * mins10
    mean = rate * expected_minutes
    residuals = [g["points"] - rate * g["minutes"] for g in last10]
    variance = sum(x * x for x in residuals) / max(1, len(residuals) - 1)
    sd = max(4.5, min(13.5, math.sqrt(variance)))
    p_over = max(0.001, min(0.999, 1.0 - _normal_cdf(row["line"], mean, sd)))
    return mean, sd, p_over


def _score(rows, weights):
    scored = []
    for row in rows:
        mean, sd, p_over = _predict(row, weights)
        p_market_over_raw = _implied(row["over_price"])
        p_market_under_raw = _implied(row["under_price"])
        p_market_over = p_market_over_raw / (p_market_over_raw + p_market_under_raw)
        target = 1.0 if row["result"] == "OVER" else 0.0
        if row["result"] == "PUSH":
            continue
        scored.append({
            **row, "projection": mean, "sd": sd, "p_over": p_over,
            "market_p_over": p_market_over, "target_over": target
        })
    return scored


def _brier(rows):
    if not rows:
        return None
    return sum((r["p_over"] - r["target_over"]) ** 2 for r in rows) / len(rows)


def _summarize(rows):
    settled = [r for r in rows if r["result"] != "PUSH"]
    wins = 0
    units = 0.0
    picks = []
    for r in settled:
        p_over = r["p_over"]
        edge_over = p_over - r["market_p_over"]
        edge_under = -edge_over
        over_ev = p_over * (_american_decimal(r["over_price"]) - 1.0) - (1.0 - p_over)
        p_under = 1.0 - p_over
        under_ev = p_under * (_american_decimal(r["under_price"]) - 1.0) - (1.0 - p_under)
        side = "OVER" if over_ev >= under_ev else "UNDER"
        ev = max(over_ev, under_ev)
        edge = edge_over if side == "OVER" else edge_under
        if ev <= 0 or edge < 0.02:
            continue
        won = r["result"] == side
        price = r["over_price"] if side == "OVER" else r["under_price"]
        units += (_american_decimal(price) - 1.0) if won else -1.0
        wins += 1 if won else 0
        picks.append({"player": r["player"], "date": r["date"][:10],
                      "side": side, "line": r["line"], "price": price,
                      "projection": round(r["projection"], 2),
                      "actual": r["actual"], "win": won,
                      "edge_pct": round(edge * 100, 2), "ev_pct": round(ev * 100, 2)})
    n = len(picks)
    return {
        "bets": n, "wins": wins, "losses": n - wins,
        "win_rate_pct": round(100.0 * wins / n, 2) if n else None,
        "net_units_flat_1u": round(units, 3),
        "roi_pct": round(100.0 * units / n, 2) if n else None,
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
            "history_sample": ({k: hsample[0].get(k) for k in ("event_id", "commence_time", "bookmaker", "line", "actual_value", "is_main_line")} if hsample else None),
            "game_sample": ({"event_id": gfirst.get("event_id"), "commence_time": gfirst.get("commence_time"),
                             "stats_type": type(gstats).__name__,
                             "stats_keys": list(gstats.keys())[:15] if isinstance(gstats, dict) else None,
                             "stats_first": gstats[:3] if isinstance(gstats, list) else None} if gsample else None)
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

    # The market's no-vig side is a benchmark, not a model pick.
    market_wins = 0
    market_units = 0.0
    market_n = 0
    for r in test_scored:
        side = "OVER" if r["market_p_over"] >= 0.5 else "UNDER"
        price = r["over_price"] if side == "OVER" else r["under_price"]
        won = r["result"] == side
        market_units += (_american_decimal(price) - 1.0) if won else -1.0
        market_wins += int(won)
        market_n += 1

    holdout = _summarize(test_scored)
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
            "prediction": "Pregame scoring rate per minute from prior L5/L10/season games multiplied by projected minutes (60% L5 / 40% L10); residual SD from prior L10, clipped 4.5–13.5 points; normal CDF for line probability.",
            "weight_selection": "Five fixed L5/L10/season scoring-rate blends compared on training Brier score only; lowest-Brier blend frozen before holdout.",
            "selection": "One side per player-game only when model EV at the actual DraftKings price is positive and model edge versus the no-vig DraftKings market is at least 2 percentage points.",
            "leakage_controls": "Game logs strictly before tip; same-day games held together; current-season regular season only; main line only; in-play re-priced lines excluded; player history actuals cross-checked against box scores."
        },
        "selected_weights_l5_l10_season": list(best_weights),
        "train_brier": round(best_brier, 6) if best_brier is not None else None,
        "holdout": holdout,
        "holdout_probability_brier": round(_brier(test_scored), 6) if test_scored else None,
        "holdout_market_baseline": {
            "bets": market_n, "wins": market_wins,
            "win_rate_pct": round(100.0 * market_wins / market_n, 2) if market_n else None,
            "net_units_flat_1u": round(market_units, 3),
            "roi_pct": round(100.0 * market_units / market_n, 2) if market_n else None
        },
        "limitations": [
            "The fixed cohort is a predeclared star list, not a claim that these are the league's top 20 by current usage.",
            "The model uses historical points and minutes only; no archived pregame injury, lineup, opponent, pace or usage feed is included.",
            "Only DraftKings main lines are evaluated; this is not a test of alternate lines or FanDuel.",
            "A 30-day Hobby event-history window may constrain available odds history; report coverage is shown per player."
        ]
    }
