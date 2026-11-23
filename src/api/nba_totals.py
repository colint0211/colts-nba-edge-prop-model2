"""NBA full-game totals engine.

Independent team-score projections, training-only total calibration, a continuous
score distribution, DraftKings-only pricing, counterfactual reasoning, immutable
daily snapshots, and final-score settlement. Legacy player-prop and ATS ledgers
are intentionally not modified.
"""
import copy
import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import game_model as gm

_ET = "America/New_York"
_MODEL_VERSION = "nba_game_totals_counterfactual_v1"
_CACHE = {"at": 0.0, "events": None, "quota": {}}
_CACHE_TTL = 60
_TEAM_CACHE = {"at": 0.0, "data": None}
_TEAM_CACHE_TTL = 300
_SOURCE_MAX_AGE_SECONDS = 300
_MIN_EV_PCT = 3.0
_TARGET_EV = 0.03
_FALLBACK_TOTAL_SD = 18.0


def _connect():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def initialize():
    with _connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nba_totals_game_snapshots (
          slate_date TEXT NOT NULL, event_id TEXT NOT NULL,
          matchup TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
          snapshot JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(slate_date,event_id)
        );
        CREATE TABLE IF NOT EXISTS nba_totals_predictions (
          id BIGSERIAL PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
          slate_date TEXT NOT NULL, event_id TEXT NOT NULL, matchup TEXT NOT NULL,
          home_team TEXT NOT NULL, away_team TEXT NOT NULL, selection TEXT NOT NULL,
          line DOUBLE PRECISION NOT NULL, odds INTEGER NOT NULL,
          model_probability DOUBLE PRECISION NOT NULL, market_probability DOUBLE PRECISION,
          projected_home DOUBLE PRECISION, projected_away DOUBLE PRECISION,
          projected_total DOUBLE PRECISION, model_version TEXT NOT NULL,
          snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), settled_at TIMESTAMPTZ,
          actual_value DOUBLE PRECISION, outcome TEXT, profit_units DOUBLE PRECISION
        );
        CREATE INDEX IF NOT EXISTS nba_totals_predictions_slate
          ON nba_totals_predictions(slate_date);
        CREATE INDEX IF NOT EXISTS nba_totals_predictions_pending
          ON nba_totals_predictions(settled_at) WHERE settled_at IS NULL;
        """)


def _team_ratings(force=False):
    """Load only the validated, free Basketball-Reference 30-team snapshot."""
    now = time.time()
    if not force and _TEAM_CACHE["data"] is not None and now - _TEAM_CACHE["at"] < _TEAM_CACHE_TTL:
        return _TEAM_CACHE["data"]
    path = Path(__file__).resolve().parents[2] / "data" / "nba_team_advanced.json"
    if not path.exists():
        raise RuntimeError("Validated Basketball-Reference team ratings snapshot is missing.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    fetched = datetime.fromisoformat(str(payload.get("fetched_at") or "").replace("Z", "+00:00"))
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds())
    current_year = datetime.now(timezone.utc).year
    season_start = current_year if datetime.now(timezone.utc).month >= 7 else current_year - 1
    expected_season = f"{season_start}-{str(season_start + 1)[-2:]}"
    teams = payload.get("teams") or {}
    required = ("ortg", "drtg", "pace")
    valid_rows = (
        isinstance(teams, dict) and len(teams) == 30
        and all(code in gm._TEAM_NAMES and all(
            key in row and math.isfinite(float(row[key])) for key in required
        ) for code, row in teams.items())
    )
    if (payload.get("team_count") != 30 or not valid_rows
            or payload.get("season") != expected_season or age > 36 * 3600):
        raise RuntimeError(
            "Basketball-Reference team snapshot failed validation "
            f"(season={payload.get('season')}, age_hours={round(age / 3600, 2)}, teams={len(teams)})."
        )
    league = {
        key: sum(float(row[key]) for row in teams.values()) / len(teams)
        for key in ("ortg", "drtg", "pace")
    }
    adjusted = {}
    for code, raw in teams.items():
        games = max(0, int(raw.get("games") or 0))
        prior_weight = max(0.12, min(0.65, 8.0 / (games + 8.0))) if games else 0.65
        row = dict(raw)
        for key in ("ortg", "drtg", "pace"):
            row[key] = float(raw[key]) * (1.0 - prior_weight) + league[key] * prior_weight
        row.update({
            "games": games, "net": row["ortg"] - row["drtg"],
            "prior_weight": prior_weight,
            "source": "Basketball-Reference season-to-date ratings shrunk toward league average",
        })
        adjusted[code] = row
    result = {
        "teams": adjusted, "season": payload["season"],
        "source": payload.get("source") or "Basketball-Reference",
        "data_quality": "basketball_reference",
        "fetched_at": payload.get("fetched_at"),
        "team_count": len(adjusted), "fallback_used": False,
        "league_average": league, "shrinkage": "8 prior games; weight clamped 12%-65%",
    }
    _TEAM_CACHE.update({"at": now, "data": result})
    return result


def _fetch_lines(force=False):
    now = time.time()
    if not force and _CACHE["events"] is not None and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["events"], "cache", _CACHE["quota"]
    key = os.getenv("PROPLINE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("PROPLINE_API_KEY is missing.")
    params = urlencode({
        "markets": "totals", "bookmakers": "draftkings",
        "includeLinks": "true", "max_age": _SOURCE_MAX_AGE_SECONDS,
    })
    request = Request(
        "https://api.prop-line.com/v1/sports/basketball_nba/odds?" + params,
        headers={"X-API-Key": key, "Accept": "application/json",
                 "User-Agent": "ColtsNBATotals/1.0"},
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
        headers = dict(response.headers.items())
    events = payload if isinstance(payload, list) else (
        payload.get("events") or payload.get("data") or payload.get("results") or []
    )
    if not isinstance(events, list):
        raise RuntimeError("PropLine did not return an NBA event list.")
    quota = {
        "daily_limit": headers.get("X-Daily-Limit"),
        "daily_used": headers.get("X-Daily-Used"),
        "daily_remaining": headers.get("X-Daily-Remaining"),
    }
    _CACHE.update({"at": now, "events": events, "quota": quota})
    return events, "live", quota


def _parse_total_lines(events):
    """Parse only DraftKings full-game main totals; spreads are not required."""
    rows = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        home = gm._canonical(event.get("home_team"))
        away = gm._canonical(event.get("away_team"))
        event_id = str(event.get("id") or "")
        commence = str(event.get("commence_time") or event.get("start_time") or "")
        if not home or not away or home == away or not event_id:
            continue
        dk = next((b for b in event.get("bookmakers") or []
                   if str(b.get("key") or "").lower() == "draftkings"), None)
        if not dk:
            continue
        totals = {}
        updated_at = None
        for market in dk.get("markets") or []:
            if not isinstance(market, dict) or market.get("suspended_at"):
                continue
            if str(market.get("key") or "").lower() != "totals":
                continue
            if str(market.get("line_type") or "main").lower() not in ("", "main"):
                continue
            if market.get("period") not in (None, "", "full_game", "game", "full"):
                continue
            if market.get("team") not in (None, ""):
                continue
            updated_at = market.get("last_update") or updated_at
            for outcome in market.get("outcomes") or []:
                side = str(outcome.get("name") or "").strip().lower()
                if side not in ("over", "under"):
                    continue
                try:
                    point = float(outcome.get("point"))
                    price = int(float(outcome.get("price")))
                except (TypeError, ValueError, OverflowError):
                    continue
                if price and 100 <= abs(price) <= 10000:
                    totals[side] = {"line": point, "odds": price}
        if set(totals) != {"over", "under"}:
            continue
        if abs(totals["over"]["line"] - totals["under"]["line"]) > 1e-9:
            continue
        rows.append({
            "event_id": event_id, "commence_time": commence,
            "home": home, "away": away,
            "matchup": gm._TEAM_NAMES[away] + " @ " + gm._TEAM_NAMES[home],
            "totals": totals, "updated_at": updated_at,
            "bookmaker": "DraftKings", "bookmaker_link": dk.get("link"),
        })
    return rows


def _load_calibration():
    """Load coefficients fitted only on prior seasons; holdout is a pass/fail audit."""
    path = Path(__file__).resolve().parents[2] / "data" / "nba_totals_backtest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fit = payload.get("calibration_fit_on_train_only") or {}
        train = payload.get("train_raw") or {}
        raw_holdout = payload.get("holdout_raw") or {}
        calibrated_holdout = payload.get("holdout_after_train_calibration") or {}
        probability_check = payload.get("holdout_probability_calibration") or {}
        offset = float(fit["total_offset_points"])
        residual_sd = float(train["total_residual_sd"])
        train_games = int(train.get("games") or 0)
        holdout_games = int(calibrated_holdout.get("games") or 0)
        holdout_bias = float(calibrated_holdout["total_bias"])
        raw_mae = float(raw_holdout["total_mae"])
        calibrated_mae = float(calibrated_holdout["total_mae"])
        probability_brier = float(probability_check["brier"])
        probability_calibration_error = float(probability_check["mean_absolute_calibration_error"])
        valid_numbers = all(math.isfinite(x) for x in (
            offset, residual_sd, holdout_bias, raw_mae, calibrated_mae,
            probability_brier, probability_calibration_error,
        ))
        # These gates are defined before the holdout is evaluated. The holdout
        # never supplies the offset or variance; it only determines eligibility.
        validated = (
            valid_numbers and train_games >= 500 and holdout_games >= 100
            and 10.0 <= residual_sd <= 30.0
            and abs(holdout_bias) <= 2.0
            and calibrated_mae <= raw_mae
            and probability_brier < 0.25
            and probability_calibration_error <= 0.06
        )
        return {
            "available": valid_numbers, "validated": validated,
            "status": "VALIDATED" if validated else "HOLDOUT_REVIEW_REQUIRED",
            "total_offset_points": offset if valid_numbers else 0.0,
            "total_residual_sd_points": residual_sd if valid_numbers else _FALLBACK_TOTAL_SD,
            "train_games": train_games, "holdout_games": holdout_games,
            "holdout_bias_points": holdout_bias if valid_numbers else None,
            "raw_holdout_mae": raw_mae if valid_numbers else None,
            "calibrated_holdout_mae": calibrated_mae if valid_numbers else None,
            "holdout_probability_brier": probability_brier if valid_numbers else None,
            "holdout_probability_calibration_error": probability_calibration_error if valid_numbers else None,
            "training_seasons": payload.get("train_seasons"),
            "holdout_season": payload.get("holdout_season"),
            "method": "Training-season additive bias correction and training residual SD; untouched holdout used only for eligibility audit.",
            "generated_at": payload.get("generated_at"),
        }
    except Exception as exc:
        return {
            "available": False, "validated": False, "status": "BACKTEST_PENDING",
            "total_offset_points": 0.0, "total_residual_sd_points": _FALLBACK_TOTAL_SD,
            "train_games": 0, "holdout_games": 0, "holdout_bias_points": None,
            "raw_holdout_mae": None, "calibrated_holdout_mae": None,
            "method": "No validated training-only calibration file is available.",
            "error": type(exc).__name__,
        }


def _american_odds(probability):
    p = float(probability)
    if not 0.0 < p < 1.0:
        return None
    return round(-100.0 * p / (1.0 - p)) if p >= 0.5 else round(100.0 * (1.0 - p) / p)


def _minimum_odds(win_probability, loss_probability, target_ev=_TARGET_EV):
    win = float(win_probability)
    loss = float(loss_probability)
    if win <= 0.0:
        return None
    required_profit = (loss + float(target_ev)) / win
    if required_profit >= 1.0:
        return int(round(required_profit * 100.0))
    return int(round(-100.0 / required_profit))


def _normal_probabilities(mean, sd, line):
    over_win, push = gm._normal_market_probs(mean, sd, line, over=True)
    under_win = max(0.0, 1.0 - over_win - push)
    decided = max(1e-12, 1.0 - push)
    return {
        "over_win": over_win, "under_win": under_win, "push": push,
        "over": over_win / decided, "under": under_win / decided,
    }


def _factor_impacts(game, ratings, schedule, injuries, baseline, calibration):
    """Counterfactual model impacts, not causal effects."""
    teams = ratings.get("teams") or {}
    if not teams:
        return []
    values = list(teams.values())
    league_pace = sum(float(x.get("pace") or 0) for x in values) / len(values)
    league_ortg = sum(float(x.get("ortg") or 0) for x in values) / len(values)
    league_drtg = sum(float(x.get("drtg") or 0) for x in values) / len(values)
    impacts = []

    def add(key, label, counterfactual):
        if not counterfactual:
            return
        impacts.append({
            "key": key, "label": label,
            "home_points_impact": round(baseline["projected_home"] - counterfactual["projected_home"], 3),
            "away_points_impact": round(baseline["projected_away"] - counterfactual["projected_away"], 3),
            "total_points_impact": round(baseline["projected_total"] - counterfactual["projected_total"], 3),
        })

    neutral_pace = copy.deepcopy(ratings)
    for row in neutral_pace["teams"].values():
        row["pace"] = league_pace
    add("pace", "Pace and possession environment",
        gm._score_game(game, neutral_pace, schedule, injuries, ratings["season"]))

    neutral_efficiency = copy.deepcopy(ratings)
    for row in neutral_efficiency["teams"].values():
        row["ortg"] = league_ortg
        row["drtg"] = league_drtg
    add("efficiency", "Offensive and defensive efficiency matchup",
        gm._score_game(game, neutral_efficiency, schedule, injuries, ratings["season"]))

    # The schedule component is isolated from the injury adjustment by
    # subtracting the schedule-neutral counterfactual with the same injuries.
    add("rest_travel", "Rest, back-to-backs and travel",
        gm._score_game(game, ratings, [], injuries, ratings["season"]))

    add("availability", "Player availability adjustments",
        gm._score_game(game, ratings, schedule, [], ratings["season"]))

    offset = float(calibration.get("total_offset_points") or 0.0)
    impacts.append({
        "key": "historical_calibration", "label": "Training-only historical bias correction",
        "home_points_impact": round(offset / 2.0, 3),
        "away_points_impact": round(offset / 2.0, 3),
        "total_points_impact": round(offset, 3),
    })
    return impacts


def _market_side(quote, side, mean, sd):
    probabilities = _normal_probabilities(mean, sd, quote["line"])
    win = probabilities[side.lower() + "_win"]
    loss = probabilities["under_win" if side == "OVER" else "over_win"]
    push = probabilities["push"]
    odds = int(quote["odds"])
    ev = gm._market_ev(win, push, odds)
    raw_over = gm._american_probability(quote["over_odds"])
    raw_under = gm._american_probability(quote["under_odds"])
    market_over = raw_over / (raw_over + raw_under) if raw_over and raw_under and raw_over + raw_under else None
    market_probability = market_over if side == "OVER" else (1.0 - market_over if market_over is not None else None)
    conditional_probability = probabilities[side.lower()]
    edge = (conditional_probability - market_probability) * 100.0 if market_probability is not None else None
    return {
        "selection": side, "line": float(quote["line"]), "odds": odds,
        "model_probability": conditional_probability,
        "model_win_probability_unconditional": win,
        "push_probability": push,
        "market_probability": market_probability,
        "edge_pp": round(edge, 3) if edge is not None else None,
        "ev_pct": round(ev * 100.0, 3),
        "fair_odds": _american_odds(conditional_probability),
        "minimum_odds_3pct_ev": _minimum_odds(win, loss),
        "loss_probability_unconditional": loss,
    }


def _score_snapshot(game, ratings, schedule, injuries, calibration, feed_age):
    model = gm._score_game(game, ratings, schedule, injuries, ratings["season"])
    if not model:
        return None
    raw_home = float(model["projected_home"])
    raw_away = float(model["projected_away"])
    raw_total = raw_home + raw_away
    offset = float(calibration.get("total_offset_points") or 0.0)
    home = raw_home + offset / 2.0
    away = raw_away + offset / 2.0
    total = home + away
    sd = float(calibration.get("total_residual_sd_points") or _FALLBACK_TOTAL_SD)
    quote = game["totals"]
    line = float(quote["over"]["line"])
    over_quote = {"line": line, "odds": quote["over"]["odds"],
                  "over_odds": quote["over"]["odds"], "under_odds": quote["under"]["odds"]}
    under_quote = {"line": line, "odds": quote["under"]["odds"],
                   "over_odds": quote["over"]["odds"], "under_odds": quote["under"]["odds"]}
    over = _market_side(over_quote, "OVER", total, sd)
    under = _market_side(under_quote, "UNDER", total, sd)
    best = max((over, under), key=lambda x: x["ev_pct"])
    injury_block = bool(model.get("injury_uncertainty"))
    fresh = feed_age <= _SOURCE_MAX_AGE_SECONDS
    rating_quality_ok = model.get("data_quality") not in ("derived_early_season", "fallback_estimated")
    qualified = (
        calibration.get("validated") is True and rating_quality_ok
        and not injury_block and fresh
        and best["ev_pct"] >= _MIN_EV_PCT and best["model_probability"] >= 0.50
    )
    pick_side = best["selection"] if qualified else None
    if not calibration.get("validated"):
        signal = "RESEARCH ONLY · historical holdout gate not passed"
    elif not rating_quality_ok:
        signal = "WITHHELD · estimated fallback team ratings"
    elif injury_block:
        signal = "WITHHELD · unpriced injury availability"
    elif not fresh:
        signal = "WITHHELD · DraftKings feed is stale"
    elif qualified:
        signal = "QUALIFIED " + best["selection"]
    else:
        signal = "NO QUALIFIED BET · below 3% EV threshold"

    factors = _factor_impacts(game, ratings, schedule, injuries, model, calibration)
    if pick_side:
        supporting = [x for x in factors if (x["total_points_impact"] > 0 if pick_side == "OVER" else x["total_points_impact"] < 0)]
        opposing = [x for x in factors if (x["total_points_impact"] < 0 if pick_side == "OVER" else x["total_points_impact"] > 0)]
    else:
        supporting = []
        opposing = []
    snapshot = {
        "event_id": game["event_id"], "commence_time": game.get("commence_time"),
        "matchup": game["matchup"], "home_team": game["home"], "away_team": game["away"],
        "projected_home": round(home, 2), "projected_away": round(away, 2),
        "projected_total": round(total, 2), "raw_projected_total": round(raw_total, 2),
        "draftkings_total": line, "over_odds": int(quote["over"]["odds"]),
        "under_odds": int(quote["under"]["odds"]), "over_probability": round(over["model_probability"] * 100, 2),
        "under_probability": round(under["model_probability"] * 100, 2),
        "push_probability": round(over["push_probability"] * 100, 2),
        "over_ev_pct": over["ev_pct"], "under_ev_pct": under["ev_pct"],
        "over_edge_pp": over["edge_pp"], "under_edge_pp": under["edge_pp"],
        "over_fair_odds": over["fair_odds"], "under_fair_odds": under["fair_odds"],
        "over_minimum_odds_3pct_ev": over["minimum_odds_3pct_ev"],
        "under_minimum_odds_3pct_ev": under["minimum_odds_3pct_ev"],
        "over_market_probability": round(over["market_probability"] * 100, 2) if over["market_probability"] is not None else None,
        "under_market_probability": round(under["market_probability"] * 100, 2) if under["market_probability"] is not None else None,
        "best_side": best["selection"], "best_ev_pct": best["ev_pct"],
        "best_probability": round(best["model_probability"] * 100, 2),
        "best_odds": best["odds"], "best_fair_odds": best["fair_odds"],
        "best_minimum_odds_3pct_ev": best["minimum_odds_3pct_ev"],
        "pick_side": pick_side, "signal": signal,
        "model_sd_points": round(sd, 3), "model_version": _MODEL_VERSION,
        "calibration_status": calibration.get("status"),
        "calibration_validated": bool(calibration.get("validated")),
        "rating_quality_ok": rating_quality_ok,
        "calibration_offset_points": round(offset, 3),
        "projected_pace": model.get("projected_pace"),
        "home_adjustments": model.get("home_adjustments"),
        "away_adjustments": model.get("away_adjustments"),
        "injury_uncertainty": injury_block,
        "rating_source": model.get("rating_source"),
        "data_quality": model.get("data_quality"),
        "factor_impacts": factors,
        "supporting_factors": supporting,
        "opposing_factors": opposing,
        "reasoning_method": "Counterfactual diagnostics: one factor group is neutralized at a time while other inputs are held fixed. These are model explanations, not causal effects.",
        "odds_source": "DraftKings via PropLine; full-game main total only",
        "odds_feed_age_seconds": round(feed_age, 1),
        "odds_source_max_age_seconds": _SOURCE_MAX_AGE_SECONDS,
        "bookmaker_link": game.get("bookmaker_link"),
    }
    return snapshot


def _settle_pending():
    initialize()
    with _connect() as conn:
        rows = conn.execute("""
          SELECT id,slate_date,event_id,home_team,away_team,selection,line,odds
          FROM nba_totals_predictions WHERE settled_at IS NULL
          ORDER BY created_at LIMIT 300
        """).fetchall()
    if not rows:
        return {"checked": 0, "settled": 0}
    try:
        schedule = gm._schedule(force=True)
    except Exception as exc:
        return {"checked": len(rows), "settled": 0, "settlement_status": "schedule_unavailable",
                "error": type(exc).__name__}
    settled = 0
    for pid, slate, event_id, home, away, selection, line, odds in rows:
        try:
            slate_day = date.fromisoformat(str(slate)[:10])
        except Exception:
            continue
        match = next((g for g in schedule if g.get("completed")
                      and g.get("home") == home and g.get("away") == away
                      and abs((datetime.fromisoformat(g["date"].replace("Z", "+00:00")).date() - slate_day).days) <= 1), None)
        if not match:
            continue
        actual = float(match["home_score"]) + float(match["away_score"])
        outcome = "PUSH" if abs(actual - float(line)) < 1e-9 else (
            "WIN" if (actual > line if selection == "OVER" else actual < line) else "LOSS"
        )
        profit = 0.0 if outcome == "PUSH" else (gm._payout(odds) if outcome == "WIN" else -1.0)
        with _connect() as conn:
            cur = conn.execute("""
              UPDATE nba_totals_predictions SET actual_value=%s,outcome=%s,
                profit_units=%s,settled_at=NOW()
              WHERE id=%s AND settled_at IS NULL
            """, (actual, outcome, profit, pid))
            settled += cur.rowcount
    return {"checked": len(rows), "settled": settled}


def _metrics():
    initialize()
    with _connect() as conn:
        row = conn.execute("""
          SELECT COUNT(*),COUNT(*) FILTER(WHERE outcome='WIN'),
            COUNT(*) FILTER(WHERE outcome='LOSS'),COUNT(*) FILTER(WHERE outcome='PUSH'),
            COALESCE(SUM(profit_units),0),COALESCE(SUM(ABS(odds)),0)
          FROM nba_totals_predictions WHERE settled_at IS NOT NULL
        """).fetchone()
    n, wins, losses, pushes, units, _ = row
    decided = wins + losses
    return {
        "settled": n, "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate_pct": round(100.0 * wins / decided, 2) if decided else None,
        "net_units": round(float(units or 0), 3),
        "roi_pct": round(100.0 * float(units or 0) / n, 2) if n else None,
        "record_type": "official locked full-game totals only; flat 1-unit stakes",
    }


def _save_snapshot(slate_date, games):
    picks = [g for g in games if g.get("pick_side")]
    picks.sort(key=lambda x: (-float(x.get("best_ev_pct") or -999), -float(x.get("best_probability") or 0)))
    picks = picks[:5]
    with _connect() as conn:
        for game in games:
            conn.execute("""
              INSERT INTO nba_totals_game_snapshots
                (slate_date,event_id,matchup,home_team,away_team,snapshot)
              VALUES(%s,%s,%s,%s,%s,%s::jsonb)
              ON CONFLICT(slate_date,event_id) DO NOTHING
            """, (slate_date, game["event_id"], game["matchup"], game["home_team"],
                  game["away_team"], json.dumps(game, separators=(",", ":"), allow_nan=False)))
        for game in picks:
            selection = game["pick_side"]
            odds = game["over_odds"] if selection == "OVER" else game["under_odds"]
            probability = game["over_probability"] if selection == "OVER" else game["under_probability"]
            market_probability = game["over_market_probability"] if selection == "OVER" else game["under_market_probability"]
            identity = "|".join([slate_date, game["event_id"], selection, str(game["draftkings_total"]), str(odds)])
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            conn.execute("""
              INSERT INTO nba_totals_predictions
                (fingerprint,slate_date,event_id,matchup,home_team,away_team,selection,line,odds,
                 model_probability,market_probability,projected_home,projected_away,projected_total,
                 model_version,snapshot)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
              ON CONFLICT(fingerprint) DO NOTHING
            """, (fingerprint, slate_date, game["event_id"], game["matchup"], game["home_team"],
                  game["away_team"], selection, game["draftkings_total"], odds,
                  float(probability) / 100.0, float(market_probability) / 100.0 if market_probability is not None else None,
                  game["projected_home"], game["projected_away"], game["projected_total"],
                  _MODEL_VERSION, json.dumps(game, separators=(",", ":"), allow_nan=False)))
    return picks


def _frozen_board(slate_date):
    with _connect() as conn:
        rows = conn.execute("""
          SELECT snapshot,created_at FROM nba_totals_game_snapshots
          WHERE slate_date=%s ORDER BY event_id
        """, (slate_date,)).fetchall()
        picks = conn.execute("""
          SELECT event_id,selection,line,odds,model_probability,market_probability,
                 projected_total,snapshot,created_at
          FROM nba_totals_predictions WHERE slate_date=%s ORDER BY id
        """, (slate_date,)).fetchall()
    games = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
    saved_picks = []
    for r in picks:
        snap = r[7] if isinstance(r[7], dict) else json.loads(r[7] or "{}")
        saved_picks.append({
            "event_id": r[0], "selection": r[1], "line": r[2], "odds": r[3],
            "model_probability": round(float(r[4]) * 100, 2),
            "market_probability": round(float(r[5]) * 100, 2) if r[5] is not None else None,
            "projected_total": r[6], "matchup": snap.get("matchup"),
            "ev_pct": snap.get("best_ev_pct"), "created_at": r[8].isoformat() if r[8] else None,
        })
    return games, saved_picks


def build_board(force=False, preview_date=None):
    initialize()
    settlement = _settle_pending()
    preview = bool(preview_date)
    slate_date = str(preview_date) if preview else gm._active_slate_date()
    date.fromisoformat(slate_date)

    if not preview:
        frozen_games, frozen_picks = _frozen_board(slate_date)
        if frozen_games:
            return {
                "slate_date": slate_date, "record_status": "LOCKED", "frozen": True,
                "games": frozen_games, "picks": frozen_picks, "game_count": len(frozen_games),
                "metrics": _metrics(), "settlement": settlement,
                "calibration": _load_calibration(),
                "source_status": {"odds": "frozen DraftKings pregame snapshot",
                                  "team_ratings": "frozen pregame snapshot"},
            }

    events, odds_source, quota = _fetch_lines(force)
    lines = _parse_total_lines(events)
    now = datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    future_games = []
    for game in lines:
        try:
            start = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
            if start > now and start.astimezone(ZoneInfo(_ET)).date().isoformat() == slate_date:
                future_games.append(game)
        except Exception:
            continue

    if not future_games:
        schedule = gm._schedule(force=False)
        scheduled = []
        for item in schedule:
            try:
                day = datetime.fromisoformat(item["date"].replace("Z", "+00:00")).astimezone(ZoneInfo(_ET)).date().isoformat()
                if day == slate_date:
                    scheduled.append(item)
            except Exception:
                continue
        future_scheduled = [g for g in scheduled if not g.get("completed")
                            and datetime.fromisoformat(g["date"].replace("Z", "+00:00")) > now]
        if future_scheduled:
            status = "WAITING_FOR_LINES"
            message = "NBA games are scheduled, but DraftKings main totals are not available yet. The slate remains open."
        elif scheduled:
            status = "MISSED_PREGAME_WINDOW"
            message = "The pregame window has passed. No picks were recreated using in-game or postgame information."
        else:
            status = "NO_GAMES"
            message = "No NBA games were found for this Eastern slate date."
        return {
            "slate_date": slate_date, "record_status": status, "frozen": False,
            "games": [], "picks": [], "game_count": 0, "metrics": _metrics(),
            "settlement": settlement, "calibration": _load_calibration(),
            "model_policy": message,
            "source_status": {"odds": "PropLine DraftKings totals (" + odds_source + ")",
                              "prop_line_quota": quota},
        }

    ratings = _team_ratings(force)
    schedule = gm._schedule(force)
    from . import index as app_index
    injury_feed = app_index._load_official_injury_feed()
    injury_meta = injury_feed.get("meta") or {}
    if injury_meta.get("status") != "fresh":
        raise RuntimeError("Fresh official NBA injury report is unavailable; refusing to lock totals.")
    injuries = injury_feed.get("records") or []
    calibration = _load_calibration()
    feed_age = max(0.0, time.time() - float(_CACHE.get("at") or time.time()))

    games = []
    unavailable = []
    for game in future_games:
        try:
            snapshot = _score_snapshot(game, ratings, schedule, injuries, calibration, feed_age)
            if snapshot is None:
                unavailable.append({"matchup": game["matchup"], "reason": "team ratings unavailable"})
                continue
            games.append(snapshot)
        except Exception as exc:
            unavailable.append({"matchup": game.get("matchup"), "reason": type(exc).__name__})

    games.sort(key=lambda x: str(x.get("commence_time") or ""))
    if not games:
        raise RuntimeError("No games could be scored from the validated team ratings.")
    picks = [g for g in games if g.get("pick_side")]
    picks.sort(key=lambda x: (-float(x.get("best_ev_pct") or -999), -float(x.get("best_probability") or 0)))
    picks = picks[:5]

    if not preview:
        _save_snapshot(slate_date, games)
        # Re-read the immutable rows in case another cron request won the insert race.
        games, picks_saved = _frozen_board(slate_date)
        picks = picks_saved
    else:
        picks = [{
            "event_id": g["event_id"], "matchup": g["matchup"],
            "selection": g["pick_side"], "line": g["draftkings_total"],
            "odds": g["best_odds"], "model_probability": g["best_probability"],
            "ev_pct": g["best_ev_pct"], "projected_total": g["projected_total"],
        } for g in picks]

    return {
        "slate_date": slate_date, "record_status": "PREVIEW" if preview else "LOCKED",
        "frozen": not preview, "games": games, "picks": picks, "game_count": len(games),
        "metrics": _metrics(), "settlement": settlement, "calibration": calibration,
        "source_status": {
            "odds": "PropLine DraftKings full-game main totals (" + odds_source + ")",
            "team_ratings": ratings.get("source"), "season": ratings.get("season"),
            "team_rating_quality": ratings.get("data_quality"),
            "team_count": len(ratings.get("teams") or {}),
            "injuries": injury_meta.get("status"), "injury_players_checked": len(injuries),
            "prop_line_quota": quota, "odds_feed_age_seconds": round(feed_age, 1),
            "unavailable_games": unavailable,
        },
        "model_policy": (
            "Independent team-efficiency and pace projection; training-only calibration; "
            "normal total distribution with push handling; DraftKings prices are used only "
            "for no-vig comparison and expected value. Official picks require the untouched "
            "holdout calibration gate and at least 3% model EV. No forced bets."
        ),
        "thresholds": {"minimum_ev_pct": _MIN_EV_PCT, "minimum_probability_pct": 50.0,
                       "odds_source_max_age_seconds": _SOURCE_MAX_AGE_SECONDS},
    }


def history(slate_date):
    initialize()
    with _connect() as conn:
        rows = conn.execute("""
          SELECT id,event_id,matchup,selection,line,odds,model_probability,market_probability,
                 projected_total,actual_value,outcome,profit_units,created_at,snapshot
          FROM nba_totals_predictions WHERE slate_date=%s ORDER BY id
        """, (str(slate_date),)).fetchall()
    output = []
    for r in rows:
        snap = r[13] if isinstance(r[13], dict) else json.loads(r[13] or "{}")
        output.append({
            "id": r[0], "event_id": r[1], "matchup": r[2], "selection": r[3],
            "line": r[4], "odds": r[5], "model_probability_pct": round(float(r[6]) * 100, 2),
            "market_probability_pct": round(float(r[7]) * 100, 2) if r[7] is not None else None,
            "projected_total": r[8], "actual_total": r[9], "outcome": r[10],
            "profit_units": r[11], "created_at": r[12].isoformat() if r[12] else None,
            "ev_pct": snap.get("best_ev_pct"), "edge_pp": snap.get("over_edge_pp") if r[3] == "OVER" else snap.get("under_edge_pp"),
        })
    return output


def metrics():
    return _metrics()


def backtest_status():
    calibration = _load_calibration()
    path = Path(__file__).resolve().parents[2] / "data" / "nba_totals_backtest.json"
    if not path.exists():
        return {"status": "PENDING", "calibration": calibration, "note": "The free public-data walk-forward job has not published its results yet."}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "UNAVAILABLE", "calibration": calibration}
    return {
        "status": calibration.get("status"), "calibration": calibration,
        "method": payload.get("method"), "source": payload.get("source"),
        "train_raw": payload.get("train_raw"),
        "holdout_raw": payload.get("holdout_raw"),
        "holdout_after_train_calibration": payload.get("holdout_after_train_calibration"),
        "holdout_probability_calibration": payload.get("holdout_probability_calibration"),
        "limitations": payload.get("limitations") or [],
        "roi_available": False, "clv_available": False,
    }
