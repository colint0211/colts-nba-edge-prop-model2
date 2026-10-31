import os
import secrets
from datetime import datetime, timedelta
import json
import math
import random
import time
import re
from difflib import SequenceMatcher
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from fastapi import FastAPI, Request as FastRequest, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse
from . import prediction_ledger
from pathlib import Path
from html.parser import HTMLParser
HTML = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
UPSTREAM = "https://colts-nba-edge-prop-model-e2oyr9l03-colin-82ad.vercel.app"
LIVE_UPSTREAM = "https://colts-nba-edge-prop-model-3xj6ghyhl-colin-82ad.vercel.app"
app = FastAPI()
BP_MARKETS = {"points": 156, "rebounds": 157, "assists": 151, "steals": 160, "threes": 162}
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML)

@app.get("/api/api-sports-status")
def api_sports_status():
    """Validate the Basketball API key using its documented no-parameter timezone endpoint."""
    api_key = os.getenv("API_SPORTS_KEY", "").strip()
    if not api_key:
        return JSONResponse({"configured": False, "valid": False, "message": "API_SPORTS_KEY is not configured."}, status_code=503)
    request = Request(
        "https://v1.basketball.api-sports.io/timezone",
        headers={"User-Agent": "Mozilla/5.0 ColtsNBAEdge/1.0", "Accept": "application/json", "x-apisports-key": api_key}
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
            headers = dict(response.headers.items())
        errors = payload.get("errors") or []
        result = payload.get("response") or []
        valid = not bool(errors) and isinstance(result, list) and len(result) > 0
        # Return API error details, but never return the API key or request headers.
        safe_errors = errors if isinstance(errors, (list, dict, str)) else str(errors)
        return {
            "configured": True,
            "valid": valid,
            "endpoint_tested": "Basketball API /timezone",
            "error_details": safe_errors,
            "results_count": payload.get("results"),
            "message": "API-Sports Basketball authentication successful." if valid else "API-Sports did not validate the key. Check error_details."
        }
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            errors = body.get("errors") or body.get("message") or body
        except Exception:
            errors = "HTTP " + str(exc.code)
        return JSONResponse({
            "configured": True, "valid": False, "http_status": exc.code,
            "endpoint_tested": "Basketball API /timezone",
            "error_details": errors,
            "message": "API-Sports rejected the request. Check error_details."
        }, status_code=502)
    except Exception as exc:
        return JSONResponse({"configured": True, "valid": False, "message": "Could not reach API-Sports: " + str(exc)[:180]}, status_code=502)

@app.get("/api/proprietary-market-board")
def proprietary_market_board(market: str = "rebounds"):
    global _BP_RATE_LIMIT_UNTIL
    market = market.lower().strip()
    if market not in STAT_FIELDS:
        raise HTTPException(status_code=400, detail="Use rebounds, assists, steals, or threes.")
    now = time.time()
    cached_board = _BP_BOARD_CACHE.get(market)
    if cached_board and now - cached_board[0] < _BP_BOARD_TTL_SECONDS:
        return JSONResponse(cached_board[1])
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BettingPros lines unavailable: API key missing.")
    market_id = BP_MARKETS[market]
    bp_payload, props_source = _bp_fetch_props(market, 50)
    field, label = STAT_FIELDS[market]
    output, failures = [], []
    props = bp_payload.get("props") or [] if isinstance(bp_payload, dict) else []
    offers_by_event = {}
    for raw in props:
        participant = raw.get("participant") or {}
        player_obj = participant.get("player") or {}
        event = raw.get("event") or {}
        event_id = event.get("id") or raw.get("event_id")
        if event_id and str(event_id) not in offers_by_event:
            offers_by_event[str(event_id)] = _bp_event_offers(event_id, market)
        over, under = raw.get("over") or {}, raw.get("under") or {}
        name = participant.get("name") or raw.get("player_name") or (raw.get("player") if isinstance(raw.get("player"), str) else None) or player_obj.get("name") or player_obj.get("full_name")
        if not name:
            continue
        quote = _bp_two_book_market(offers_by_event.get(str(event_id), []), name) if event_id else None
        line = quote["line"] if quote else over.get("line", under.get("line", raw.get("line", raw.get("line_value"))))
        try:
            line = float(line)
        except (TypeError, ValueError):
            continue
        over_odds = quote["over_odds"] if quote else over.get("odds", raw.get("over_odds"))
        under_odds = quote["under_odds"] if quote else under.get("odds", raw.get("under_odds"))
        try:
            athlete_id = _espn_player_id(name)
            if not athlete_id:
                failures.append(name + ": ESPN player match unavailable")
                continue
            values, sample_label, player_minutes = _player_history(athlete_id, field, label)
            team_for_dvp = player_obj.get("team") or participant.get("team_abbreviation") or participant.get("team") or ""
            event_matchup = event.get("name") or raw.get("event_name") or raw.get("matchup") or _bp_event_matchup(event_id)
            matchup_adjustment = _dvp_matchup_adjustment(athlete_id, team_for_dvp, event_matchup, market, name)
            model = _simulate_market(values, line, None, None, _norm_player_name(name) + market, minutes=player_minutes, market=market, matchup_adjustment=matchup_adjustment)
            if not model:
                failures.append(name + ": fewer than five valid game logs")
                continue
            if quote:
                model = _score_blended_market(model, quote)
            else:
                model["recommendation"] = "PASS"
                model["expected_value_pct"] = None
                model["best_side"] = None
                model["best_probability"] = None
                model["best_odds"] = None
                model["edge_pp"] = None
                model["blend_method"] = "No verified DK/FD two-sided quote at a matching line; no bet rating"

            # Apply the same minimum sample rule used by the points board.
            # A small sample can still produce a visible projection, but never
            # a positive-EV recommendation or a Top 5 EV entry.
            sample_size = int(model.get("sample_size") or len(values))
            if sample_size < 8:
                model["confidence"] = "low"
                model["recommendation"] = "PASS"
                model["expected_value_pct"] = None
                model["best_side"] = None
                model["best_probability"] = None
                model["best_odds"] = None
                model["edge_pp"] = None
                model["probability_note"] = (
                    "Projection only: fewer than 8 verified regular-season games. "
                    "EV ranking is disabled until the sample grows."
                )
            else:
                model.setdefault("confidence", "provisional")
            output.append({
                "player": name,
                "team": player_obj.get("team") or participant.get("team_abbreviation") or participant.get("team") or "",
                "game": event_matchup,
                "market": market, "line": line, "over_odds": over_odds, "under_odds": under_odds,
                "sample_label": sample_label, "athlete_id": athlete_id, "games_used": len(values),
                "position": (matchup_adjustment or {}).get("position"),
                "defense_vs_position": matchup_adjustment,
                **model,
            })
        except Exception as exc:
            failures.append(name + ": " + type(exc).__name__ + ": " + str(exc)[:180])
    output.sort(key=lambda x: (x.get("expected_value_pct") is None, -(x.get("expected_value_pct") or -999)))
    try:
        prediction_ledger.initialize()
    except Exception:
        pass
    result = {
        "market": market,
        "source": "ESPN game-log distribution + BettingPros DraftKings/FanDuel no-vig market blend",
        "blend": "50/50 logarithmic opinion pool (provisional pending historical calibration)",
        "line_count": len(props), "projected_count": len(output), "props": output,
        "unavailable": failures[:20],
        "cache_ttl_seconds": _BP_BOARD_TTL_SECONDS,
    }
    _BP_BOARD_CACHE[market] = (time.time(), result)
    return JSONResponse(result)


@app.get("/api/bettingpros-books")
def bettingpros_books():
    """Return active BettingPros sportsbook IDs/names so book IDs are never guessed."""
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BETTINGPROS_API_KEY is missing.")
    req = Request(
        "https://api.bettingpros.com/v3/books?sport=NBA",
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail="BettingPros books endpoint returned HTTP " + str(exc.code)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve BettingPros sportsbook IDs.") from exc
    books = payload.get("books", []) if isinstance(payload, dict) else []
    return JSONResponse({"books": books, "count": len(books), "source": "BettingPros API"})


@app.get("/api/bettingpros-offers")
def bettingpros_offers(market: str = "rebounds", event_id: int | None = None):
    """Expose current offers for supported NBA prop markets, preserving per-book prices."""
    market = market.lower().strip()
    market_id = BP_MARKETS.get(market)
    if market_id is None:
        raise HTTPException(status_code=400, detail="Unsupported NBA prop market.")
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BETTINGPROS_API_KEY is missing.")
    params = {"sport": "NBA", "market_id": market_id, "page": 1, "limit": 50}
    if event_id is not None:
        params["event_id"] = event_id
    req = Request(
        "https://api.bettingpros.com/v3/offers?" + urlencode(params),
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail="BettingPros offers endpoint returned HTTP " + str(exc.code)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve BettingPros offers.") from exc
    rows = payload.get("offers", []) if isinstance(payload, dict) else []
    return JSONResponse({"market": market, "market_id": market_id, "offers": rows,
                         "pagination": payload.get("_pagination", {}) if isinstance(payload, dict) else {},
                         "source": "BettingPros API"})


@app.get("/api/bettingpros-props")
def bettingpros_props(market: str = "rebounds"):
    market = market.lower().strip()
    market_id = BP_MARKETS.get(market)
    if market_id is None:
        raise HTTPException(status_code=400, detail="Unsupported market. Use points, rebounds, assists, steals, or threes.")
    payload, cache_status = _bp_fetch_props(market, 50)
    return JSONResponse({
        "market": market, "market_id": market_id,
        "props": payload.get("props", []),
        "pagination": payload.get("_pagination", {}),
        "source": "BettingPros API",
        "cache_status": cache_status,
        "cache_ttl_seconds": _BP_CACHE_TTL_SECONDS
    })

def _quarter_name_key(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _enrich_live_quarters(payload):
    """Attach player quarter point totals only when ESPN supplies actual scoring plays."""
    if not isinstance(payload, dict):
        return payload
    games = payload.get("games") or []
    by_id = payload.get("players_by_id") or {}
    by_name = payload.get("players_by_name") or {}
    if not isinstance(by_id, dict) or not isinstance(by_name, dict):
        return payload
    player_objects = []
    seen = set()
    for collection in (by_id, by_name):
        for key, player in collection.items():
            if not isinstance(player, dict) or id(player) in seen:
                continue
            seen.add(id(player))
            player_objects.append((str(key), player))
    for game in games:
        if not isinstance(game, dict) or str(game.get("state") or "").lower() not in ("in", "post", "final"):
            continue
        event_id = game.get("event_id") or game.get("id")
        if not event_id:
            continue
        try:
            summary_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?" + urlencode({"event": str(event_id)})
            req = Request(summary_url, headers={"User-Agent": "Mozilla/5.0 ColtNBAEdge/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=5) as response:
                summary = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            continue
        plays = summary.get("scoringPlays") or []
        if not plays:
            plays = (summary.get("plays") or {}).get("scoringPlays") or []
        totals_by_id = {}
        totals_by_name = {}
        for play in plays:
            if not isinstance(play, dict):
                continue
            period = play.get("period") or {}
            try:
                quarter = int(period.get("number") if isinstance(period, dict) else period)
                points = int(float(play.get("scoreValue") or 0))
            except (TypeError, ValueError):
                continue
            if quarter < 1 or quarter > 4 or points <= 0:
                continue
            scorer = play.get("athlete") or play.get("scorer")
            if isinstance(scorer, dict):
                pid = scorer.get("id")
                name = scorer.get("displayName") or scorer.get("fullName") or scorer.get("name")
            else:
                pid = play.get("athleteId") or play.get("scorerId")
                name = play.get("athleteName") or play.get("scorerName")
            if not pid and not name:
                participants = play.get("participants") or play.get("athletesInvolved") or []
                for part in participants:
                    if not isinstance(part, dict):
                        continue
                    role = str(part.get("role") or part.get("type") or "").lower()
                    athlete = part.get("athlete") if isinstance(part.get("athlete"), dict) else part
                    if "scor" in role or "shooter" in role:
                        scorer = athlete
                        pid = athlete.get("id")
                        name = athlete.get("displayName") or athlete.get("fullName") or athlete.get("name")
                        break
            if pid:
                row = totals_by_id.setdefault(str(pid), [0, 0, 0, 0])
                row[quarter - 1] += points
            elif name:
                row = totals_by_name.setdefault(_quarter_name_key(name), [0, 0, 0, 0])
                row[quarter - 1] += points
        if not totals_by_id and not totals_by_name:
            continue
        for key, player in player_objects:
            pid = str(player.get("athlete_id") or player.get("id") or key)
            name = player.get("player") or player.get("name") or player.get("displayName") or ""
            qpoints = totals_by_id.get(pid) or totals_by_name.get(_quarter_name_key(name))
            if qpoints is not None:
                player["points_by_quarter"] = qpoints
    return payload

@app.get("/api/model/diagnostics")
def model_diagnostics():
    """Safe database diagnostics; never returns connection strings or credentials."""
    import re
    result = {
        "database_url_configured": bool(os.getenv("DATABASE_URL")),
        "postgres_url_configured": bool(os.getenv("POSTGRES_URL")),
        "psycopg_importable": False,
        "connection_ok": False,
        "table_exists": False,
    }
    try:
        import psycopg
        result["psycopg_importable"] = True
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:240]
        return JSONResponse(result, status_code=503)
    try:
        with prediction_ledger._connect() as conn:
            result["connection_ok"] = True
            row = conn.execute("SELECT to_regclass('public.nba_predictions')").fetchone()
            result["table_exists"] = bool(row and row[0])
    except Exception as exc:
        message = str(exc)
        message = re.sub(r'postgres(?:ql)?://[^\s]+', '[redacted database URL]', message, flags=re.I)
        result["error_type"] = type(exc).__name__
        result["error"] = message[:300]
        return JSONResponse(result, status_code=503)
    return JSONResponse(result)



def _run_pending_settlements():
    """Settle only completed ESPN games with exact athlete and matchup verification."""
    market_fields = {
        "points": ("points", "PTS"), "rebounds": ("rebounds", "REB"),
        "assists": ("assists", "AST"), "steals": ("steals", "STL"),
        "threes": ("threePointsMade", "3PT"),
    }
    team_aliases = {"NY": "NYK", "GS": "GSW", "WSH": "WAS"}
    event_team_cache = {}
    result = {"checked": 0, "settled": 0, "skipped": 0, "errors": 0, "settlements": []}

    def matchup_for_event(event_id):
        if not event_id:
            return None
        key = str(event_id)
        if key not in event_team_cache:
            try:
                payload = _get_json(
                    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?"
                    + urlencode({"event": key}), timeout=8
                )
                competitions = (payload.get("header") or {}).get("competitions") or []
                competitors = competitions[0].get("competitors") if competitions else []
                teams = set()
                for competitor in competitors or []:
                    team = competitor.get("team") or {}
                    code = str(team.get("abbreviation") or "").upper()
                    if code:
                        teams.add(team_aliases.get(code, code))
                event_team_cache[key] = teams if len(teams) == 2 else None
            except Exception:
                event_team_cache[key] = None
        return event_team_cache[key]

    try:
        prediction_ledger.initialize()
        candidates = prediction_ledger.list_settlement_candidates(2000)
        result["checked"] = len(candidates)
        game_log_cache = {}
        for candidate in candidates:
            snap = candidate.get("snapshot") or {}
            athlete_id = str(snap.get("athlete_id") or "").strip()
            market = str(candidate.get("market") or "").lower()
            mapping = market_fields.get(market)
            game = str(snap.get("game") or "").upper().replace(" ", "")
            created_date = str(candidate.get("created_date") or "")
            # Provider labels may append weekday/tipoff text, e.g. CHA@WASFRI7:00.
            # Match only the leading two team abbreviations before validating ESPN.
            matchup = re.match(r"^([A-Z]{2,3})@([A-Z]{2,3})", game)
            if not athlete_id.isdigit() or not mapping or not created_date or not matchup:
                result["skipped"] += 1
                continue
            expected_teams = {
                team_aliases.get(code, code) for code in matchup.groups()
            }
            if len(expected_teams) != 2:
                result["skipped"] += 1
                continue
            field, label = mapping
            try:
                log_key = (athlete_id, field, label, 2027)
                if log_key not in game_log_cache:
                    game_log_cache[log_key] = _gamelog_records(athlete_id, field, label, 2027)
                records = game_log_cache[log_key]
                possible = []
                created = datetime.fromisoformat(created_date).date()
                for row in records:
                    row_date = str(row.get("date") or "")[:10]
                    event_id = row.get("event_id")
                    if not row_date or not event_id:
                        continue
                    try:
                        game_date = datetime.fromisoformat(row_date).date()
                    except ValueError:
                        continue
                    # Picks may be entered up to two days before tipoff.
                    if not (created <= game_date <= created + timedelta(days=2)):
                        continue
                    if matchup_for_event(event_id) == expected_teams:
                        possible.append(row)
                # More than one possible event is ambiguous; do not settle it.
                if len(possible) != 1:
                    result["skipped"] += 1
                    continue
                actual = float(possible[0]["value"])
                settled = prediction_ledger.settle_prediction(candidate["id"], actual)
                result["settled"] += 1
                result["settlements"].append({
                    "id": candidate["id"], "player": candidate["player"],
                    "market": market, "actual": actual, "outcome": settled["outcome"],
                })
            except ValueError:
                result["skipped"] += 1
            except Exception:
                result["errors"] += 1
        return JSONResponse(result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Prediction database unavailable.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Automatic settlement could not run.") from exc

@app.post("/api/model/settle-pending")
async def settle_pending_predictions(request: FastRequest):
    secret = os.getenv("CRON_SECRET", "")
    if not secret or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _run_pending_settlements()

@app.get("/api/cron/settle-pending")
async def cron_settle_pending(request: FastRequest):
    secret = os.getenv("CRON_SECRET", "")
    if not secret or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _run_pending_settlements()


@app.post("/api/model/daily-top5")
async def save_daily_top5(request: FastRequest):
    try:
        payload = await request.json()
        prediction_ledger.initialize()
        result = prediction_ledger.save_daily_top_five(
            payload.get("props") or [], payload.get("market"), payload.get("slate_date")
        )
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not save daily Top 5 picks.") from exc


@app.get("/api/model/daily-top5")
def get_daily_top5(slate_date: str, market: str = "all"):
    try:
        prediction_ledger.initialize()
        return JSONResponse({
            "predictions": prediction_ledger.list_daily_top_five(slate_date, market),
            "all_time": prediction_ledger.daily_top_five_metrics(),
        })
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Daily Top 5 history is temporarily unavailable.") from exc


@app.get("/api/model/ledger")
def model_ledger(status: str = "unsettled", market: str = "all", limit: int = 100):
    try:
        prediction_ledger.initialize()
        return JSONResponse({"predictions": prediction_ledger.list_predictions(status, market, limit)})
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Prediction database unavailable.") from exc

@app.get("/api/model/metrics")
def model_metrics(market: str = "all"):
    try:
        prediction_ledger.initialize()
        return JSONResponse(prediction_ledger.metrics(market))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not calculate calibration metrics.") from exc
@app.post("/api/model/settle")
async def model_settle(request: FastRequest):
    try:
        payload = await request.json()
        prediction_id = int(payload.get("prediction_id"))
        prediction_ledger.initialize()
        return JSONResponse(prediction_ledger.settle_prediction(prediction_id, payload.get("actual_value")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not settle prediction.") from exc

@app.get("/api/custom-lines")
async def custom_lines():
    """Return the latest persisted custom line and odds for each player/market/event."""
    try:
        prediction_ledger.initialize()
        return JSONResponse({"props": prediction_ledger.list_custom_lines()})
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Saved custom lines are temporarily unavailable.") from exc

@app.post("/api/model/recalculate")
async def model_recalculate(request: FastRequest):
    """Reprice one player prop at the user's actual sportsbook line and two-sided odds."""
    try:
        payload = await request.json()
        market = str(payload.get("market") or "").lower().strip()
        if market not in ("points", *STAT_FIELDS.keys()):
            raise HTTPException(status_code=400, detail="Unsupported market.")
        player = str(payload.get("player") or "").strip()
        athlete_id = str(payload.get("athlete_id") or "").strip()
        line = float(payload.get("line"))
        over_odds = int(payload.get("over_odds"))
        under_odds = int(payload.get("under_odds"))
        if not player or not athlete_id or not math.isfinite(line):
            raise HTTPException(status_code=400, detail="Player, athlete ID, and a valid line are required.")
        if over_odds == 0 or under_odds == 0 or (over_odds < 0 and abs(over_odds) < 100) or (under_odds < 0 and abs(under_odds) < 100):
            raise HTTPException(status_code=400, detail="Enter valid American odds (for example -110 or +100).")
        if abs(over_odds) > 10000 or abs(under_odds) > 10000:
            raise HTTPException(status_code=400, detail="Odds are outside the supported range.")
        field, label = ("points", "PTS") if market == "points" else STAT_FIELDS[market]
        values, sample_label, minutes = _player_history(athlete_id, field, label)
        matchup_adjustment = _dvp_matchup_adjustment(athlete_id, payload.get("team"), payload.get("game"), market, player)
        model = _simulate_market(values, line, None, None, _norm_player_name(player) + market + "custom", minutes=minutes, market=market, matchup_adjustment=matchup_adjustment)
        if not model:
            raise HTTPException(status_code=422, detail="Not enough verified game-log history to recalculate this player.")
        over_raw, under_raw = _american_probability(over_odds), _american_probability(under_odds)
        market_mass = over_raw + under_raw
        quote = {"over_probability": over_raw/market_mass, "under_probability": under_raw/market_mass,
                 "over_odds": over_odds, "under_odds": under_odds, "books_used": ["User-entered sportsbook"],
                 "over_book": "User-entered", "under_book": "User-entered"}
        model = _score_blended_market(model, quote)
        model.update({"player": player, "team": str(payload.get("team") or ""), "game": str(payload.get("game") or ""),
                      "market": market, "line": line, "over_odds": over_odds, "under_odds": under_odds,
                      "athlete_id": athlete_id, "sample_label": sample_label, "games_used": len(values),
                      "position": (matchup_adjustment or {}).get("position"),
                      "expected_profit_per_100": model.get("expected_value_pct"), "custom_line": True,
                      "line_source": "User-entered sportsbook line and odds"})
        prediction_ledger.initialize()
        saved = prediction_ledger.save_predictions([model], market)
        return JSONResponse({"prop": model, "ledger": saved})
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Enter a numeric line and valid American over/under odds.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Player history or prediction database is temporarily unavailable.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not recalculate and save this custom line.") from exc

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def api_proxy(path: str, request: FastRequest):
    base = LIVE_UPSTREAM if path == "live-stats" else UPSTREAM
    url = base + "/api/" + path
    if request.url.query:
        url += "?" + request.url.query
    headers = {"accept": request.headers.get("accept", "application/json")}
    secret = os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET")
    if secret:
        headers["x-vercel-protection-bypass"] = secret
    body = await request.body()
    if request.headers.get("content-type"):
        headers["content-type"] = request.headers["content-type"]
    req = Request(url, data=body if body else None, headers=headers, method=request.method)
    try:
        with urlopen(req, timeout=25) as response:
            response_body = response.read()
            media_type = response.headers.get("content-type", "application/json")
            if path == "auto-board" and "json" in media_type.lower():
                try:
                    board_payload = json.loads(response_body.decode("utf-8", "replace"))
                    props = board_payload.get("props") or []

                    def _canonical_team(code):
                        code = str(code or "").strip().upper()
                        return {"NY": "NYK", "GS": "GSW", "WSH": "WAS"}.get(code, code)

                    def _game_team_codes(game):
                        match = re.match(r"^\s*([A-Z]{2,3})@([A-Z]{2,3})", str(game or "").upper())
                        return {_canonical_team(match.group(1)), _canonical_team(match.group(2))} if match else set()

                    projection_fields = (
                        "projection", "sd", "games_used", "recent5_avg", "sample_season",
                        "sample_label", "confidence", "probability_reliability",
                        "raw_over_probability", "over_probability", "under_probability",
                        "best_side", "best_odds", "best_probability", "implied_probability",
                        "edge_pp", "expected_profit_per_100", "expected_value_pct",
                        "recommendation", "projected_minutes", "push_probability",
                        "simulations", "sample_size", "long_run_mean", "recent5_mean",
                        "recent10_mean", "weighted_mean", "distribution_std_dev",
                        "minute_adjustment_factor", "minutes_sample_size",
                        "baseline_minutes", "minutes_method", "calibration_status",
                    )

                    for prop in props:
                        if not isinstance(prop, dict):
                            continue

                        # The upstream feed can return a name that appears on
                        # multiple team rosters. Never guess which athlete row
                        # is intended or let an upstream value appear bettable.
                        ambiguous_identity = (
                            prop.get("model_status") == "roster_mismatch"
                            or "multiple rosters" in str(prop.get("reason") or "").lower()
                            or "identity is ambiguous" in str(prop.get("reason") or "").lower()
                        )
                        game_teams = _game_team_codes(prop.get("game"))
                        roster_team = _canonical_team(prop.get("roster_team") or prop.get("team"))

                        # Re-resolve ambiguous upstream names against the actual
                        # ESPN rosters for this matchup. Only an exact name found
                        # on exactly one of the two teams is accepted.
                        if ambiguous_identity and game_teams:
                            resolved_id, resolved_team = _espn_roster_player_id(
                                prop.get("player"), game_teams
                            )
                            if resolved_id and resolved_team:
                                prop["athlete_id"] = resolved_id
                                prop["roster_team"] = resolved_team
                                prop["identity_resolution"] = "exact_matchup_roster"
                                prop["identity_verified"] = True
                                prop["model_status"] = "unprojected"
                                prop["reason"] = "Identity resolved by exact ESPN matchup-roster match."
                                roster_team = _canonical_team(resolved_team)
                                ambiguous_identity = False

                        wrong_game_team = bool(roster_team and game_teams and roster_team not in game_teams)
                        if ambiguous_identity or wrong_game_team:
                            for key in projection_fields:
                                prop.pop(key, None)
                            prop["model_status"] = "unprojected"
                            prop["identity_verified"] = False
                            prop["expected_profit_per_100"] = None
                            prop["expected_value_pct"] = None
                            prop["recommendation"] = "PASS"
                            prop["best_side"] = None
                            prop["reason"] = (
                                "Player identity is ambiguous across rosters; excluded from projections."
                                if ambiguous_identity else
                                "Roster team does not match either team in the listed matchup; excluded from projections."
                            )
                            continue

                        if roster_team and game_teams:
                            prop["identity_verified"] = True

                        if prop.get("line") is None:
                            continue
                        athlete_id = prop.get("athlete_id")
                        if not athlete_id:
                            continue
                        try:
                            values, sample_label, player_minutes = _player_history(athlete_id, "points", "PTS")
                            matchup_adjustment = _dvp_matchup_adjustment(athlete_id, roster_team, prop.get("game"), "points", prop.get("player"))
                            model = _simulate_market(
                                values, float(prop["line"]), prop.get("over_odds"),
                                prop.get("under_odds"),
                                _norm_player_name(prop.get("player")) + "points",
                                minutes=player_minutes, market="points", matchup_adjustment=matchup_adjustment
                            )
                            if not model:
                                # Never leave an upstream projection/probability on a row
                                # after the local model has rejected its history.
                                for key in (
                                    "projection", "sd", "games_used", "recent5_avg", "sample_season",
                                    "sample_label", "confidence", "probability_reliability",
                                    "raw_over_probability", "over_probability", "under_probability",
                                    "best_side", "best_odds", "best_probability", "implied_probability",
                                    "edge_pp", "expected_profit_per_100", "recommendation",
                                    "projected_minutes", "push_probability", "simulations",
                                    "expected_value_pct", "model_components", "model_method",
                                    "sample_size", "long_run_mean", "recent5_mean", "recent10_mean",
                                    "weighted_mean", "distribution_std_dev", "minute_adjustment_factor",
                                    "minutes_sample_size", "baseline_minutes", "minutes_method",
                                    "calibration_status"
                                ):
                                    prop.pop(key, None)
                                prop["model_status"] = "unprojected"
                                prop["reason"] = "Insufficient verified regular-season game-log history for the local points model."
                                continue
                            prop.update(model)
                            prop["position"] = (matchup_adjustment or {}).get("position")
                            prop["sample_label"] = sample_label
                            prop["games_used"] = model.get("sample_size")
                            prop["recent5_avg"] = model.get("recent5_mean")
                            prop["raw_over_probability"] = model.get("over_probability")
                            # expected_value_pct is already expressed as percent return.
                            # For a $100 stake, the dollar EV has the same numeric value.
                            prop["expected_profit_per_100"] = (
                                round(float(model["expected_value_pct"]), 2)
                                if model.get("expected_value_pct") is not None else None
                            )
                            prop["implied_probability"] = None
                            prop["probability_reliability"] = None
                            prop["probability_note"] = "Local empirical game-log simulation; not calibrated until settled predictions are evaluated."
                            prop["model_status"] = "projected"
                            sample_size = int(model.get("sample_size") or 0)
                            prop["confidence"] = "low" if sample_size < 8 else "provisional"
                            if sample_size < 8:
                                # Keep the early-season projection visible, but do
                                # not promote a 4-7 game sample into an EV wager.
                                prop["expected_value_pct"] = None
                                prop["expected_profit_per_100"] = None
                                prop["edge_pp"] = None
                                prop["best_side"] = None
                                prop["best_odds"] = None
                                prop["best_probability"] = None
                                prop["recommendation"] = "PASS"
                                prop["probability_note"] = (
                                    "Projection only: fewer than 8 verified regular-season games. "
                                    "EV ranking is disabled until the sample grows."
                                )
                            else:
                                prop["recommendation"] = (
                                    "VALUE CANDIDATE" if model.get("expected_value_pct") is not None and model["expected_value_pct"] > 0.05
                                    else "LEAN / VERIFY" if model.get("expected_value_pct") is not None and model["expected_value_pct"] > 0
                                    else "PASS"
                                )
                            if prop.get("injury_flag") or prop.get("injury_status"):
                                prop["recommendation"] = "PASS"
                                # Availability overrides the mathematical edge:
                                # retain the projection for reference, but never
                                # display a positive EV or a suggested side.
                                prop["expected_value_pct"] = None
                                prop["expected_profit_per_100"] = None
                                prop["edge_pp"] = None
                                prop["best_side"] = None
                                prop["best_odds"] = None
                                prop["best_probability"] = None
                                prop["probability_note"] = (
                                    "No bet: player is listed unavailable or has an injury status. "
                                    "Recheck availability before using this projection."
                                )
                        except Exception as model_error:
                            prop["model_status"] = "unprojected"
                            prop["reason"] = "Local points model could not verify player history: " + type(model_error).__name__
                    board_payload["props"] = props
                    board_payload["projected_count"] = sum(1 for p in props if isinstance(p, dict) and p.get("model_status") == "projected")
                    board_payload["unprojected_count"] = sum(1 for p in props if isinstance(p, dict) and p.get("model_status") != "projected")
                    board_payload["roster_mismatch_count"] = sum(1 for p in props if isinstance(p, dict) and p.get("identity_verified") is False)
                    board_payload["model"] = "Local recency-weighted empirical distribution; ESPN game logs; minutes adjustment when available; roster identity validation"
                    response_body = json.dumps(board_payload, separators=(",", ":")).encode("utf-8")
                    prediction_ledger.initialize()
                    # The dashboard records only its official Top 5 selections.
                except Exception:
                    pass
            if path == "live-stats" and "json" in media_type.lower():
                try:
                    live_payload = json.loads(response_body.decode("utf-8", "replace"))
                    live_payload = _enrich_live_quarters(live_payload)
                    response_body = json.dumps(live_payload, separators=(",", ":")).encode("utf-8")
                except Exception:
                    pass
            return Response(content=response_body, status_code=response.status, media_type=media_type)
    except HTTPError as e:
        return Response(content=e.read(), status_code=e.code, media_type=e.headers.get("content-type", "application/json"))
    except (URLError, TimeoutError) as e:
        raise HTTPException(status_code=502, detail="Upstream API unavailable") from e