"""Historical DraftKings ATS validation against an untouched walk-forward holdout.

The upstream aligned backtest supplies out-of-sample team-only margin forecasts.
This module joins those forecasts to DraftKings closing spreads and final scores
from DunkAPI. It never uses market prices to create model probabilities.
"""
import json
import math
import os
from datetime import date
from urllib.request import Request, urlopen

API_URL = "https://mcp.dunkapi.com/v1/get_odds"
MIN_EDGE_PP = 2.5
MIN_EV_PCT = 1.5
TEAM_ALIASES = {
    "GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP",
    "WSH": "WAS", "BRK": "BKN", "PHO": "PHX",
}


def canonical_team(value):
    code = str(value or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def american_probability(odds):
    try:
        value = float(odds)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    return -value / (-value + 100) if value < 0 else 100 / (value + 100)


def american_profit(odds):
    value = float(odds)
    return 100.0 / abs(value) if value < 0 else value / 100.0


def normal_cdf(value, mean, sd):
    return 0.5 * (1.0 + math.erf((value - mean) / (sd * math.sqrt(2.0))))


def cover_probabilities(mean_margin, home_line, sd):
    """Return unconditional home win-against-spread probability and push mass."""
    line = float(home_line)
    if abs(line - round(line)) < 1e-9:
        boundary = -line
        push = normal_cdf(boundary + 0.5, mean_margin, sd) - normal_cdf(boundary - 0.5, mean_margin, sd)
        win = 1.0 - normal_cdf(boundary + 0.5, mean_margin, sd)
    else:
        win = 1.0 - normal_cdf(-line, mean_margin, sd)
        push = 0.0
    return max(0.0, min(1.0, win)), max(0.0, min(1.0, push))


def market_ev(probability, push_probability, odds):
    loss = max(0.0, 1.0 - probability - push_probability)
    return probability * american_profit(odds) - loss


def parse_api_payload(payload):
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    games = data.get("games", []) if isinstance(data, dict) else []
    if not isinstance(games, list):
        raise RuntimeError("DunkAPI get_odds response did not contain a games list.")
    return games


def fetch_day(date_string, api_key):
    date.fromisoformat(date_string)
    body = json.dumps({"date": date_string, "league": "nba"}).encode("utf-8")
    request = Request(API_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-key": api_key,
        "User-Agent": "ColtsNBAEdge/2.0",
    }, method="POST")
    with urlopen(request, timeout=35) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    return parse_api_payload(payload)


def _draftkings_quote(game):
    books = game.get("books") or []
    matches = [b for b in books if "draftkings" in str(b.get("provider", "")).lower().replace(" ", "")]
    for book in matches:
        line = book.get("home_spread_close")
        home_odds = book.get("spread_home_odds")
        away_odds = book.get("spread_away_odds")
        if line is None or american_probability(home_odds) is None or american_probability(away_odds) is None:
            continue
        return {"line": float(line), "home_odds": int(home_odds), "away_odds": int(away_odds),
                "provider": book.get("provider")}
    return None


def score_game(prediction, game, sd):
    home = canonical_team((game.get("home") or {}).get("abbrev"))
    away = canonical_team((game.get("away") or {}).get("abbrev"))
    if home != canonical_team(prediction.get("home")) or away != canonical_team(prediction.get("away")):
        return None
    quote = _draftkings_quote(game)
    if not quote:
        return None
    actual_home = (game.get("home") or {}).get("score")
    actual_away = (game.get("away") or {}).get("score")
    if actual_home is None or actual_away is None:
        return None
    actual_margin = float(actual_home) - float(actual_away)
    mean_margin = float(prediction["calibrated_margin"])
    home_win, push = cover_probabilities(mean_margin, quote["line"], sd)
    away_win, away_push = cover_probabilities(-mean_margin, -quote["line"], sd)
    raw_home = american_probability(quote["home_odds"])
    raw_away = american_probability(quote["away_odds"])
    denominator = raw_home + raw_away
    if denominator <= 0:
        return None
    home_market = raw_home / denominator
    away_market = raw_away / denominator
    candidates = []
    for side, probability, market_probability, side_push, odds, signed_margin, signed_line in (
        ("home", home_win, home_market, push, quote["home_odds"], mean_margin, quote["line"]),
        ("away", away_win, away_market, away_push, quote["away_odds"], -mean_margin, -quote["line"]),
    ):
        ev = market_ev(probability, side_push, odds)
        edge = (probability - market_probability) * 100.0
        if edge >= MIN_EDGE_PP and ev * 100.0 >= MIN_EV_PCT and probability >= 0.50:
            result = actual_margin + signed_line
            outcome = "PUSH" if abs(result) < 1e-9 else ("WIN" if result > 0 else "LOSS")
            profit = 0.0 if outcome == "PUSH" else (american_profit(odds) if outcome == "WIN" else -1.0)
            candidates.append({
                "date": prediction["date"], "game_id": prediction["game_id"],
                "matchup": f"{away} @ {home}", "selection": side, "line": signed_line,
                "odds": odds, "model_probability": probability,
                "market_no_vig_probability": market_probability, "edge_pp": edge,
                "ev_pct": ev * 100.0, "outcome": outcome, "profit_units": profit,
                "actual_margin": actual_margin, "predicted_margin": mean_margin,
                "closing_line_provider": quote["provider"],
            })
    return max(candidates, key=lambda x: x["ev_pct"]) if candidates else None


def summarize(picks):
    decided = [p for p in picks if p["outcome"] != "PUSH"]
    wins = sum(p["outcome"] == "WIN" for p in picks)
    losses = sum(p["outcome"] == "LOSS" for p in picks)
    pushes = sum(p["outcome"] == "PUSH" for p in picks)
    risked = len(picks)
    net = sum(p["profit_units"] for p in picks)
    brier = (sum((p["model_probability"] - (1.0 if p["outcome"] == "WIN" else 0.0)) ** 2
                 for p in decided) / len(decided)) if decided else None
    return {
        "bets": risked, "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate_pct_ex_push": round(100.0 * wins / len(decided), 2) if decided else None,
        "net_units_flat_1u": round(net, 3),
        "roi_pct_on_risked_units": round(100.0 * net / risked, 2) if risked else None,
        "mean_edge_pp": round(sum(p["edge_pp"] for p in picks) / risked, 2) if risked else None,
        "mean_ev_pct_at_close": round(sum(p["ev_pct"] for p in picks) / risked, 2) if risked else None,
        "ats_brier_ex_push": round(brier, 5) if brier is not None else None,
    }


def run(input_path="nba_live_aligned_backtest.json", output_path="nba_historical_ats_backtest.json"):
    api_key = os.getenv("DUNKAPI_API_KEY", "").strip()
    opt_in = os.getenv("NBA_ATS_BACKTEST_ENABLED", "").strip().lower() in ("1", "true", "yes")
    if not api_key or not opt_in:
        result = {
            "status": "skipped_pending_explicit_opt_in_and_api_key",
            "required": ["DUNKAPI_API_KEY GitHub Actions secret", "NBA_ATS_BACKTEST_ENABLED=true repository variable"],
            "note": "No API calls or credits were used. DunkAPI get_odds costs 10 credits per date request.",
        }
        with open(output_path, "w", encoding="utf-8") as output:
            json.dump(result, output, indent=2)
        print("HISTORICAL_ATS_BACKTEST=" + json.dumps(result, sort_keys=True))
        return result

    with open(input_path, "r", encoding="utf-8") as source:
        aligned = json.load(source)
    predictions = aligned.get("holdout_predictions") or []
    sd = float((aligned.get("calibration_fit_on_train_only") or {}).get("margin_residual_sd_points") or 0)
    if not predictions or sd <= 0:
        raise RuntimeError("Aligned holdout predictions or training-only margin residual SD are missing.")
    dates = sorted({str(p["date"]) for p in predictions})
    estimated_credits = len(dates) * 10
    print(f"Historical ATS backfill: {len(dates)} dates; estimated DunkAPI cost {estimated_credits} credits.")
    odds_games = []
    for day in dates:
        odds_games.extend(fetch_day(day, api_key))
    odds_index = {}
    for game in odds_games:
        key = (str(game.get("date")), canonical_team((game.get("home") or {}).get("abbrev")),
               canonical_team((game.get("away") or {}).get("abbrev")))
        odds_index[key] = game
    picks = []
    matched = 0
    dk_quoted = 0
    for prediction in predictions:
        key = (str(prediction["date"]), canonical_team(prediction.get("home")),
               canonical_team(prediction.get("away")))
        game = odds_index.get(key)
        if not game:
            continue
        matched += 1
        if _draftkings_quote(game):
            dk_quoted += 1
        pick = score_game(prediction, game, sd)
        if pick:
            picks.append(pick)
    result = {
        "status": "complete",
        "source": "DunkAPI get_odds; DraftKings closing spread and paired closing prices",
        "holdout_season": aligned.get("holdout_season"),
        "train_seasons": aligned.get("train_seasons"),
        "dates_requested": len(dates),
        "estimated_credits_used": estimated_credits,
        "holdout_games": len(predictions),
        "games_matched_to_odds_feed": matched,
        "games_with_draftkings_closing_spread_and_prices": dk_quoted,
        "eligible_model_bets": len(picks),
        "thresholds": {"minimum_edge_pp": MIN_EDGE_PP, "minimum_ev_pct": MIN_EV_PCT, "minimum_probability": 0.50},
        "probability_sd_points_from_training_only": sd,
        "metrics": summarize(picks),
        "picks": picks,
        "limitations": [
            "This is an injury-free team-form baseline test: historical pregame injury reports and the live player-impact snapshot are not replayed.",
            "Only DraftKings closing spread quotes with both side prices are included; missing games are excluded and coverage is reported.",
            "The walk-forward forecast is formula-aligned but does not reproduce historical Basketball-Reference snapshots, rest/travel inputs or current live injury adjustments.",
            "Closing-line backtest results are diagnostic; they do not establish future profitability or live closing-line value.",
        ],
    }
    with open(output_path, "w", encoding="utf-8") as output:
        json.dump(result, output, indent=2, allow_nan=False)
    print("HISTORICAL_ATS_BACKTEST=" + json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    run()
