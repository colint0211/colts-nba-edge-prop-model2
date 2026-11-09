import os
import secrets
from datetime import datetime, timedelta, timezone
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
from fastapi.responses import HTMLResponse, Response, JSONResponse as StarletteJSONResponse

def _json_safe(value):
    """Convert nested values to strict JSON-compatible types before responding."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value

class JSONResponse(StarletteJSONResponse):
    def render(self, content):
        return super().render(_json_safe(content))
from . import prediction_ledger, game_model, advanced_metrics
from pathlib import Path
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
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


_BDL_STATUS_CACHE = {"at": 0.0, "data": None}
_BDL_STATUS_CACHE_TTL = 900

_BDL_GAMES_CACHE = {"at": 0.0, "data": None}
_BDL_GAMES_CACHE_TTL = 600


def _load_balldontlie_games(allow_fetch=True):
    """Load today's and tomorrow's NBA games once per cache window.

    BALLDONTLIE free tier is used only as a secondary schedule/status source;
    it never supplies player projections or changes a betting probability.
    """
    now = time.time()
    cached = _BDL_GAMES_CACHE.get("data")
    cached_ttl = (
        _BDL_GAMES_CACHE_TTL
        if isinstance(cached, dict) and cached.get("status") == "ok"
        else 60
    )
    if cached is not None and now - _BDL_GAMES_CACHE.get("at", 0.0) < cached_ttl:
        return cached
    if not allow_fetch:
        return {"status": "not_loaded", "source": "BALLDONTLIE", "games": [], "count": 0}

    api_key = os.getenv("BALLDONTLIE_API_KEY", "").strip()
    if not api_key:
        data = {"status": "not_configured", "source": "BALLDONTLIE", "games": [], "count": 0}
        _BDL_GAMES_CACHE.update({"at": now, "data": data})
        return data

    today = datetime.now(ZoneInfo("America/New_York")).date()
    dates = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    params = [("dates[]", day) for day in dates] + [("per_page", 100)]
    request = Request(
        "https://api.balldontlie.io/v1/games?" + urlencode(params),
        headers={
            "Authorization": api_key,
            "Accept": "application/json",
            "User-Agent": "ColtsNBAEdge/1.0",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
            remaining = response.headers.get("X-RateLimit-Remaining")
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("BALLDONTLIE games response did not contain a data list.")

        games = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            home = row.get("home_team") or {}
            visitor = row.get("visitor_team") or {}
            home_code = _canonical_team_code(home.get("abbreviation"))
            visitor_code = _canonical_team_code(visitor.get("abbreviation"))
            if not home_code or not visitor_code:
                continue
            games.append({
                "id": row.get("id"),
                "date": row.get("date"),
                "datetime": row.get("datetime"),
                "status": row.get("status"),
                "period": row.get("period"),
                "time": row.get("time"),
                "home_team": home_code,
                "visitor_team": visitor_code,
                "home_team_score": row.get("home_team_score"),
                "visitor_team_score": row.get("visitor_team_score"),
                "postseason": bool(row.get("postseason")),
            })
        data = {
            "status": "ok",
            "source": "BALLDONTLIE NBA API",
            "dates_checked": dates,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count": len(games),
            "rate_limit_remaining": remaining,
            "games": games,
        }
    except HTTPError as exc:
        data = {
            "status": "rate_limited" if exc.code == 429 else "unavailable",
            "source": "BALLDONTLIE NBA API",
            "dates_checked": dates,
            "count": 0,
            "error": "Rate limit reached." if exc.code == 429 else "BALLDONTLIE returned HTTP " + str(exc.code) + ".",
            "games": [],
        }
    except Exception as exc:
        data = {
            "status": "unavailable",
            "source": "BALLDONTLIE NBA API",
            "dates_checked": dates,
            "count": 0,
            "error": type(exc).__name__,
            "games": [],
        }

    _BDL_GAMES_CACHE.update({"at": now, "data": data})
    return data


def _apply_balldontlie_game(prop, feed):
    """Attach a verified matchup's schedule/status without changing model output."""
    if not isinstance(prop, dict) or not isinstance(feed, dict) or feed.get("status") != "ok":
        return
    matchup_codes = _team_codes_from_game(prop.get("game"))
    if len(matchup_codes) != 2:
        return
    match = None
    for game in feed.get("games") or []:
        if {game.get("home_team"), game.get("visitor_team")} == matchup_codes:
            match = game
            break
    if not match:
        return
    prop["balldontlie_game"] = {
        "source": "BALLDONTLIE",
        "game_id": match.get("id"),
        "date": match.get("date"),
        "datetime": match.get("datetime"),
        "status": match.get("status"),
        "period": match.get("period"),
        "clock": match.get("time"),
        "home_team": match.get("home_team"),
        "visitor_team": match.get("visitor_team"),
        "home_score": match.get("home_team_score"),
        "visitor_score": match.get("visitor_team_score"),
    }


@app.get("/api/balldontlie-games")
def balldontlie_games():
    """Diagnostic view of cached BALLDONTLIE schedule/status data."""
    feed = _load_balldontlie_games()
    return JSONResponse({key: value for key, value in feed.items() if key != "games"} | {"games": feed.get("games", [])})




@app.get("/api/balldontlie-status")
def balldontlie_status():
    """Check the three BALLDONTLIE endpoints included in the free NBA tier."""
    now = time.time()
    cached = _BDL_STATUS_CACHE.get("data")
    if cached is not None and now - _BDL_STATUS_CACHE.get("at", 0.0) < _BDL_STATUS_CACHE_TTL:
        return JSONResponse(cached)

    api_key = os.getenv("BALLDONTLIE_API_KEY", "").strip()
    if not api_key:
        return JSONResponse({
            "configured": False,
            "valid": False,
            "message": "BALLDONTLIE_API_KEY is not configured in this deployment."
        }, status_code=503)

    local_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    probes = [
        ("teams", "https://api.balldontlie.io/v1/teams?per_page=100"),
        ("players", "https://api.balldontlie.io/v1/players?search=LeBron&per_page=5"),
        ("games", "https://api.balldontlie.io/v1/games?" + urlencode({"dates[]": local_date, "per_page": 100})),
    ]
    results = {}
    remaining = None

    for name, url in probes:
        request = Request(
            url,
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "User-Agent": "ColtsNBAEdge/1.0",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
                headers = response.headers
            rows = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(rows, dict):
                count = 1
            elif isinstance(rows, list):
                count = len(rows)
            else:
                count = 0
            results[name] = {"ok": isinstance(rows, (list, dict)), "records": count}
            rate_remaining = headers.get("X-RateLimit-Remaining")
            if rate_remaining is not None:
                remaining = rate_remaining
        except HTTPError as exc:
            results[name] = {
                "ok": False,
                "http_status": exc.code,
                "error": "Unauthorized or endpoint unavailable for this account tier." if exc.code == 401
                         else "Rate limit reached." if exc.code == 429
                         else "BALLDONTLIE returned HTTP " + str(exc.code) + ".",
            }
            rate_remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if rate_remaining is not None:
                remaining = rate_remaining
        except Exception as exc:
            results[name] = {"ok": False, "error": "Request failed: " + type(exc).__name__}

    valid = all(item.get("ok") for item in results.values())
    output = {
        "configured": True,
        "valid": valid,
        "source": "BALLDONTLIE NBA API",
        "tier_scope_tested": "free-tier endpoints",
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "game_date_et": local_date,
        "endpoints": results,
        "rate_limit_remaining": remaining,
        "cache_ttl_seconds": _BDL_STATUS_CACHE_TTL,
        "message": "All three free-tier endpoints authenticated successfully." if valid
                   else "One or more free-tier endpoint checks failed; see endpoint results.",
    }
    _BDL_STATUS_CACHE.update({"at": now, "data": output})
    return JSONResponse(output)


@app.get("/api/injury-status")
def official_injury_status():
    feed = _load_official_injury_feed()
    return JSONResponse({
        "source": feed.get("meta", {}).get("source"),
        "status": feed.get("meta", {}).get("status"),
        "report_timestamp_et": feed.get("meta", {}).get("report_timestamp_et"),
        "fetched_at_utc": feed.get("meta", {}).get("fetched_at_utc"),
        "age_minutes": feed.get("meta", {}).get("age_minutes"),
        "record_count": feed.get("meta", {}).get("record_count", 0),
        "season": feed.get("meta", {}).get("season"),
        "injuries": feed.get("records", []),
    })

ESPN_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
ESPN_SEARCH = "https://site.web.api.espn.com/apis/search/v2"
STAT_FIELDS = {
    "points": ("points", "PTS"),
    "rebounds": ("rebounds", "REB"),
    "assists": ("assists", "AST"),
    "steals": ("steals", "STL"),
    "threes": ("threePointsMade", "3PT"),
}
_ESPN_ID_CACHE = {}
_ESPN_LOG_CACHE = {}
_ESPN_ROSTER_CACHE = {}

_ESPN_POSITION_CACHE = {}
_API_SPORTS_POSITION_CACHE = {}
_API_SPORTS_POSITION_CACHE_TTL = 86400
_DVP_CACHE = {"at": 0.0, "data": None, "season": None}
_DVP_STAT_CACHE = {}
_DVP_CACHE_TTL = 3600
_TEAM_NAMES = {
 "ATL":"Atlanta Hawks","BOS":"Boston Celtics","BKN":"Brooklyn Nets","CHA":"Charlotte Hornets","CHI":"Chicago Bulls",
 "CLE":"Cleveland Cavaliers","DAL":"Dallas Mavericks","DEN":"Denver Nuggets","DET":"Detroit Pistons","GSW":"Golden State Warriors",
 "HOU":"Houston Rockets","IND":"Indiana Pacers","LAC":"LA Clippers","LAL":"Los Angeles Lakers","MEM":"Memphis Grizzlies",
 "MIA":"Miami Heat","MIL":"Milwaukee Bucks","MIN":"Minnesota Timberwolves","NOP":"New Orleans Pelicans","NYK":"New York Knicks",
 "OKC":"Oklahoma City Thunder","ORL":"Orlando Magic","PHI":"Philadelphia 76ers","PHX":"Phoenix Suns","POR":"Portland Trail Blazers",
 "SAC":"Sacramento Kings","SAS":"San Antonio Spurs","TOR":"Toronto Raptors","UTA":"Utah Jazz","WAS":"Washington Wizards"
}
_TEAM_ALIASES = {name.lower(): code for code, name in _TEAM_NAMES.items()}
_TEAM_ALIASES.update({"los angeles clippers":"LAC","brooklyn":"BKN","new york":"NYK","golden state":"GSW",
 "phoenix":"PHX","new orleans":"NOP","san antonio":"SAS"})
class _DVPHTMLParser(HTMLParser):
 def __init__(self):
  super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.text=[]
 def handle_starttag(self,tag,attrs):
  tag=tag.lower()
  if tag=="tr": self.row=[]
  elif tag in ("td","th") and self.row is not None: self.cell=[]
 def handle_data(self,data):
  value=str(data or "").strip()
  if value:
   self.text.append(value)
   if self.cell is not None: self.cell.append(value)
 def handle_endtag(self,tag):
  tag=tag.lower()
  if tag in ("td","th") and self.cell is not None and self.row is not None:
   self.row.append(" ".join(self.cell).strip()); self.cell=None
  elif tag=="tr" and self.row is not None:
   if self.row: self.rows.append(list(self.row))
   self.row=None; self.cell=None
def _canonical_team_code(value):
 if isinstance(value,dict):
  value=value.get("abbreviation") or value.get("shortDisplayName") or value.get("displayName") or value.get("name") or ""
 raw=str(value or "").strip(); upper=raw.upper()
 aliases={"NY":"NYK","GS":"GSW","WSH":"WAS","PHO":"PHX","NO":"NOP","SA":"SAS"}
 if upper in _TEAM_NAMES: return upper
 if upper in aliases: return aliases[upper]
 return _TEAM_ALIASES.get(raw.lower())
def _team_codes_from_game(game):
 text=str(game or "").upper()
 match=re.search(r"\b([A-Z]{2,3})\s*(?:@|VS\.?|V)\s*([A-Z]{2,3})\b",text)
 if match:
  return {x for x in (_canonical_team_code(match.group(1)),_canonical_team_code(match.group(2))) if x}
 lower=str(game or "").lower(); found=set()
 for name,code in _TEAM_ALIASES.items():
  if name in lower: found.add(code)
 return found

_TANK01_DVP_TABLE_CACHE = {"at": 0.0, "tables": None, "meta": None}
_TANK01_DVP_TABLE_TTL = 21600


def _fetch_tank01_dvp_tables():
 """Fetch all five positional defense markets from one Tank01 request."""
 now = time.time()
 cached = _TANK01_DVP_TABLE_CACHE
 if cached.get("tables") is not None and now - cached.get("at", 0.0) < _TANK01_DVP_TABLE_TTL:
  return cached["tables"], cached["meta"]
 payload, headers = _tank01_request("/getNBATeams", {"teamStats": "true"}, timeout=20)
 body = payload.get("body")
 if isinstance(body, list):
  teams = body
 elif isinstance(body, dict):
  teams = [value for value in body.values() if isinstance(value, dict)]
 else:
  teams = []
 if len(teams) < 30:
  raise RuntimeError("Tank01 did not return all 30 NBA teams.")
 stat_keys = {"PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl", "3PM": "tptfgm"}
 positions = ("PG", "SG", "SF", "PF", "C")
 tables = {stat: {} for stat in stat_keys}
 game_counts = []
 for team in teams:
  if not isinstance(team, dict):
   continue
  team_code = _canonical_team_code(team.get("teamAbv"))
  defense = team.get("defensiveStats")
  if not team_code or not isinstance(defense, dict):
   continue
  try:
   games = int(float(defense.get("gamesPlayed")))
  except (TypeError, ValueError, OverflowError):
   games = 0
  if games > 0:
   game_counts.append(games)
  for stat, field in stat_keys.items():
   position_values = defense.get(field)
   if not isinstance(position_values, dict):
    continue
   for position in positions:
    try:
     value = float(position_values.get(position))
    except (TypeError, ValueError, OverflowError):
     continue
    if math.isfinite(value) and value >= 0:
     tables[stat][(team_code, position)] = {stat: value}
 for stat, table in tables.items():
  if len(table) < 150:
   raise RuntimeError("Tank01 positional table is incomplete for " + stat + ".")
 if len(game_counts) < 30:
  raise RuntimeError("Tank01 did not provide a games-played sample for every team.")
 meta = {
  "min_games_played": min(game_counts),
  "max_games_played": max(game_counts),
  "team_count": len(game_counts),
  "source": "Tank01 RapidAPI getNBATeams teamStats=true",
  "rate_limit_remaining": headers.get("x-ratelimit-requests-remaining"),
 }
 _TANK01_DVP_TABLE_CACHE.update({"at": now, "tables": tables, "meta": meta})
 return tables, meta


def _fetch_dvp_table(stat="PTS"):
 stat=str(stat or "PTS").upper().strip()
 stat_aliases={"POINTS":"PTS","REBOUNDS":"REB","ASSISTS":"AST","STEALS":"STL","THREES":"3PM"}
 stat=stat_aliases.get(stat,stat)
 if stat not in ("PTS","REB","AST","STL","3PM"):
  raise ValueError("Unsupported DVP statistic: "+stat)
 now=time.time()
 cached=_DVP_STAT_CACHE.get(stat)
 if cached and now-cached["at"]<_DVP_CACHE_TTL:
  return cached["data"]

 def fetch_and_parse(timeframe):
  url="https://www.nbahouse.net/defense-vs-position?stat="+stat+"&tf="+timeframe
  request=Request(url,headers={"User-Agent":"Mozilla/5.0 ColtsNBAEdge/1.0","Accept":"text/html"})
  with urlopen(request,timeout=12) as response:
   html=response.read().decode("utf-8","replace")
  parser=_DVPHTMLParser(); parser.feed(html)
  positions=("PG","SG","SF","PF","C"); parsed={}
  for cells in parser.rows:
   if len(cells)<6: continue
   team_text=cells[0].strip().lower()
   code=_TEAM_ALIASES.get(team_text)
   if not code: code=_TEAM_ALIASES.get(re.sub(r"^[a-z]{3}","",team_text).strip())
   if not code: continue
   for position,cell in zip(positions,cells[1:6]):
    match=re.search(r"([0-9]+(?:\.[0-9]+)?)",str(cell))
    if match: parsed[(code,position)]={stat:float(match.group(1))}
  if len(parsed)<120:
   raise RuntimeError("Individual-position "+stat+" defense-vs-position source returned an incomplete table.")
  return parsed," ".join(parser.text)

 # Use the current season as soon as Tank01 supplies a complete table.
 # Early-season samples are intentionally accepted and labeled as small samples.
 try:
  tank_tables,tank_meta=_fetch_tank01_dvp_tables()
  tank_table=tank_tables.get(stat) or {}
  if len(tank_table)>=150:
   parsed=tank_table
   season="Tank01 current season ("+str(tank_meta["min_games_played"])+" games minimum per team; early-season sample)"
  else:
   parsed,_=fetch_and_parse("prev")
   season="previous season (Tank01 current-season table incomplete)"
 except Exception:
  parsed,_=fetch_and_parse("prev")
  season="previous season (Tank01 current-season data unavailable)"
 _DVP_STAT_CACHE[stat]={"at":now,"data":parsed,"season":season}
 if stat=="PTS":
  _DVP_CACHE.update({"at":now,"data":parsed,"season":season})
 return parsed

def _position_group(position):
 if isinstance(position,dict):
  raw=(position.get("abbreviation") or position.get("displayName") or position.get("name") or "")
 else:
  raw=position or ""
 p=str(raw).strip().upper()
 aliases={
  "POINT GUARD":"PG","POINT-GUARD":"PG","PG":"PG",
  "SHOOTING GUARD":"SG","SG":"SG",
  "SMALL FORWARD":"SF","SF":"SF",
  "POWER FORWARD":"PF","PF":"PF",
  "CENTER":"C","C":"C",
  "GUARD":"G","G":"G","FORWARD":"F","F":"F",
 }
 return aliases.get(p)



def _api_sports_player_position(player_name):
 """Return API-Sports Basketball's listed broad position (Guard/Forward/Center).
 This source does not distinguish PG/SG or SF/PF; ESPN remains the finer-position source.
 """
 name=str(player_name or "").strip()
 normalized=_norm_player_name(name)
 if len(normalized)<3:
  return None
 now=time.time()
 cached=_API_SPORTS_POSITION_CACHE.get(normalized)
 if cached and now-cached.get("at",0)<_API_SPORTS_POSITION_CACHE_TTL:
  return cached.get("data")
 api_key=os.getenv("API_SPORTS_KEY","").strip()
 if not api_key:
  _API_SPORTS_POSITION_CACHE[normalized]={"at":now,"data":None}
  return None
 try:
  query=urlencode({"search":name})
  request=Request(
   "https://v1.basketball.api-sports.io/players?"+query,
   headers={"User-Agent":"Mozilla/5.0 ColtsNBAEdge/1.0","Accept":"application/json","x-apisports-key":api_key}
  )
  with urlopen(request,timeout=8) as response:
   payload=json.loads(response.read().decode("utf-8","replace"))
  rows=payload.get("response") or [] if isinstance(payload,dict) else []
  exact=[]
  for row in rows:
   if not isinstance(row,dict):
    continue
   candidate=row.get("name") or row.get("player",{}).get("name") if isinstance(row.get("player"),dict) else row.get("name")
   if _norm_player_name(candidate)==normalized:
    position=_position_group(row.get("position"))
    if position:
     exact.append(position)
  # Require an exact, unambiguous player-name match. Never guess from fuzzy search results.
  unique=set(exact)
  result=next(iter(unique)) if len(unique)==1 else None
  _API_SPORTS_POSITION_CACHE[normalized]={"at":now,"data":result}
  return result
 except Exception:
  _API_SPORTS_POSITION_CACHE[normalized]={"at":now,"data":None}
  return None

def _espn_player_position(athlete_id,team_code):
 """Resolve a specific PG/SG/SF/PF/C role; generic G/F roster labels are not enough."""
 key=(str(athlete_id),str(team_code or "").upper())
 if key in _ESPN_POSITION_CACHE: return _ESPN_POSITION_CACHE[key]
 code=_canonical_team_code(team_code)
 if not code: return None
 found=None
 generic_position=None
 try:
  payload=_get_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/"+code.lower()+"/roster",timeout=8)
  def walk(value):
   nonlocal found,generic_position
   if found not in (None,"G","F"): return
   if isinstance(value,dict):
    athlete=value.get("athlete") if isinstance(value.get("athlete"),dict) else value
    if str(athlete.get("id") or "").strip()==str(athlete_id):
     position=athlete.get("position") or value.get("position") or {}
     resolved=_position_group(position)
     if resolved in ("G","F"): generic_position=resolved
     elif resolved: found=resolved
     return
    for child in value.values():
     if isinstance(child,(dict,list)): walk(child)
   elif isinstance(value,list):
    for child in value: walk(child)
  walk(payload)
 except Exception:
  pass

 # ESPN's roster can expose only G/F. Check its athlete profile for a
 # specific PG/SG/SF/PF/C role before falling back to a blended group.
 if found in (None,"G","F"):
  try:
   athlete=_get_json(
    "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/"+str(athlete_id),
    timeout=8
   )
   specific=_position_group(athlete.get("position") if isinstance(athlete,dict) else None)
   if specific and specific not in ("G","F"): found=specific
   elif specific in ("G","F"): generic_position=specific
  except Exception:
   pass
 if not found: found=generic_position
 _ESPN_POSITION_CACHE[key]=found
 return found
def _dvp_matchup_adjustment(athlete_id,player_team,game,market,player_name=None):
 def unavailable(reason, **details):
  result={"available":False,"adjustment_applied":False,"reason":reason}
  result.update(details)
  return result
 try:
  market=str(market or "").lower().strip()
  stat_fields={"points":"PTS","rebounds":"REB","assists":"AST","steals":"STL","threes":"3PM"}
  stat=stat_fields.get(market)
  if not stat: return unavailable("Unsupported prop category; no positional-defense table is mapped for this market.")
  team_code=_canonical_team_code(player_team)
  game_teams=_team_codes_from_game(game)
  if len(game_teams)!=2:
   return unavailable("Matchup could not be parsed into two verified NBA teams.", player_team=team_code or None)
  if not team_code and player_name:
   # PropLine often omits a team suffix. Resolve the exact player name against
   # only the two verified matchup rosters; never infer from name or position.
   try:
    roster_id, roster_team = _espn_roster_player_id(player_name, game_teams)
    if roster_id and roster_team in game_teams:
     team_code = roster_team
   except Exception:
    team_code = None
  if not team_code:
   return unavailable("Player team was absent from the prop quote and could not be uniquely verified on either matchup roster.", matchup_teams=game_teams)
  if team_code not in game_teams:
   return unavailable("Player team does not match either team in the supplied matchup.", player_team=team_code, matchup_teams=game_teams)
  opponent=next((code for code in game_teams if code!=team_code),None)
  if not opponent: return unavailable("Opponent could not be identified from the matchup.")
  espn_position=_espn_player_position(athlete_id,team_code)
  api_position=None
  position_source="ESPN roster/profile"
  position_confidence="high" if espn_position not in (None,"G","F") else "medium"
  position_crosscheck="not_needed_for_specific_position"
  if espn_position in ("G","F") or espn_position is None:
   api_position=_api_sports_player_position(player_name)
   if espn_position in ("G","F"):
    if api_position and api_position!=espn_position:
     return unavailable("ESPN and API-Sports broad position labels conflict; adjustment withheld.",player_team=team_code,opponent=opponent,espn_position=espn_position,api_sports_position=api_position)
    position_crosscheck="confirmed" if api_position==espn_position else "unavailable"
    position_source="ESPN broad position"+(" + API-Sports confirmed" if api_position==espn_position else "")
    position_confidence="medium" if api_position==espn_position else "low"
   elif api_position:
    espn_position=api_position
    position_source="API-Sports Basketball fallback"
    position_confidence="medium"
    position_crosscheck="fallback_used"
  if not espn_position:
   return unavailable("Player position was not found in ESPN or the exact-name API-Sports lookup.",player_team=team_code,opponent=opponent)
  position_keys={"G":("PG","SG"),"F":("SF","PF")}.get(espn_position,(espn_position,))
  try:
   table=_fetch_dvp_table(stat)
  except Exception as exc:
   return unavailable("Positional-defense data source failed: "+type(exc).__name__+".",player_team=team_code,opponent=opponent,position=espn_position,stat=market)
  allowed_values=[table.get((opponent,pos),{}).get(stat) for pos in position_keys]
  if any(value is None for value in allowed_values):
   return unavailable("The defensive table did not contain the opponent's required position rows.",player_team=team_code,opponent=opponent,position=espn_position,stat=market)
  allowed=sum(allowed_values)/len(allowed_values)
  league_values=[row[stat] for (team,pos),row in table.items() if pos in position_keys and stat in row]
  if len(league_values)<20:
   return unavailable("The positional-defense table has too few league comparison rows.",player_team=team_code,opponent=opponent,position=espn_position,stat=market)
  league_average=sum(league_values)/len(league_values)
  if league_average<=0:
   return unavailable("The positional-defense league average is invalid.",player_team=team_code,opponent=opponent,position=espn_position,stat=market)
  ratio=allowed/league_average
  factor=max(0.92,min(1.08,1.0+(ratio-1.0)*0.30))
  position_names={"PG":"Point Guard","SG":"Shooting Guard","SF":"Small Forward","PF":"Power Forward","C":"Center","G":"Guard (PG/SG blended)","F":"Forward (SF/PF blended)"}
  stat_labels={"points":"points","rebounds":"rebounds","assists":"assists","steals":"steals","threes":"3-pointers made"}
  stat_units={"points":"PPG","rebounds":"RPG","assists":"APG","steals":"SPG","threes":"3PM"}
  season=_DVP_STAT_CACHE.get(stat,{}).get("season") or "season not identified"
  position_method="PG/SG 50/50 blend" if espn_position=="G" else "SF/PF 50/50 blend" if espn_position=="F" else "Individual listed position"
  blend_weights={position_keys[0]:1.0} if len(position_keys)==1 else {position_keys[0]:0.5,position_keys[1]:0.5}
  return {"available":True,"adjustment_applied":True,"position":espn_position,"position_group":espn_position,"position_name":position_names[espn_position],
   "position_source":position_source,"position_confidence":position_confidence,"position_crosscheck":position_crosscheck,
   "blend_weights":blend_weights,"opponent":opponent,"opponent_name":_TEAM_NAMES.get(opponent,opponent),
   "stat":market,"stat_label":stat_labels[market],"stat_unit":stat_units[market],
   "allowed_per_game":round(allowed,2),"league_position_average":round(league_average,2),
   "league_relative_ratio":round(ratio,3),"adjustment_pct":round((factor-1.0)*100,2),"factor":round(factor,4),
   "source":"NBA House individual-position "+stat+" allowed table","source_season":season,
   "method":position_method+" "+stat_labels[market]+" allowed per game vs matching-position league average; 30% dampening; adjustment capped at +/-8%"}
 except Exception as exc:
  return unavailable("Positional-defense calculation failed: "+type(exc).__name__+".")

def _espn_roster_player_id(name, team_codes):
    """Resolve an exact player identity only from the two teams in the matchup."""
    normalized_name = _norm_player_name(name)
    matches = {}

    for team_code in sorted(set(team_codes or [])):
        team_code = str(team_code or "").strip().lower()
        if not team_code:
            continue
        if team_code not in _ESPN_ROSTER_CACHE:
            url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/" + team_code + "/roster"
            try:
                payload = _get_json(url, timeout=8)
            except Exception:
                payload = {}
            found = {}

            def walk_roster(value):
                if isinstance(value, dict):
                    athlete = value.get("athlete") if isinstance(value.get("athlete"), dict) else value
                    athlete_id = str(athlete.get("id") or "").strip()
                    athlete_name = (
                        athlete.get("fullName") or athlete.get("displayName")
                        or athlete.get("name") or ""
                    )
                    if athlete_id.isdigit() and athlete_name:
                        found[athlete_id] = str(athlete_name)
                    for child in value.values():
                        if isinstance(child, (dict, list)):
                            walk_roster(child)
                elif isinstance(value, list):
                    for child in value:
                        walk_roster(child)

            walk_roster(payload)
            _ESPN_ROSTER_CACHE[team_code] = found

        for athlete_id, athlete_name in _ESPN_ROSTER_CACHE[team_code].items():
            candidate_key = _norm_player_name(athlete_name)
            score = SequenceMatcher(None, normalized_name, candidate_key).ratio()
            if candidate_key == normalized_name:
                score = 1.0
            if score >= 0.86:
                matches[(athlete_id, team_code.upper())] = (athlete_name, score)

    ranked = sorted(matches.items(), key=lambda item: item[1][1], reverse=True)
    if ranked:
        (athlete_id, team_code), (_, best_score) = ranked[0]
        second_score = ranked[1][1][1] if len(ranked) > 1 else 0.0
        # Accept fuzzy aliases only when the best roster match is clearly unique.
        if best_score >= 0.86 and (len(ranked) == 1 or best_score - second_score >= 0.06):
            canonical = {"NYK": "NYK", "GSW": "GSW"}.get(team_code, team_code)
            return athlete_id, canonical
    return None, None

def _get_json(url, timeout=12):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 ColtsNBAEdge/1.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace").replace("\\n", " ")
        raise RuntimeError("ESPN HTTP " + str(exc.code) + ": " + detail[:220]) from exc

def _norm_player_name(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

_INJURY_FEED_URL = "https://raw.githubusercontent.com/colint0211/colts-nba-edge-prop-model2/injury-feed/data/nba_injuries_latest.json"
_INJURY_FEED_CACHE = {"at": 0.0, "data": None}
_INJURY_FEED_TTL_SECONDS = 120
_INJURY_SOURCE_LABEL = "NBA official team injury report (nbainjuries)"
_INJURY_BLOCK_STATUSES = {"out", "doubtful", "questionable", "suspended", "suspension", "not with team", "inactive"}

def _injury_name_key(value):
    raw = str(value or "").strip()
    if "," in raw:
        parts = [part.strip() for part in raw.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            raw = parts[1] + " " + parts[0]
    return _norm_player_name(raw)

def _load_official_injury_feed():
    now = time.time()
    cached = _INJURY_FEED_CACHE.get("data")
    if cached is not None and now - _INJURY_FEED_CACHE.get("at", 0) < _INJURY_FEED_TTL_SECONDS:
        return cached
    try:
        request = Request(
            _INJURY_FEED_URL + "?v=" + str(int(now // 60)),
            headers={"User-Agent": "ColtsNBAEdge/1.0", "Accept": "application/json", "Cache-Control": "no-cache"},
        )
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError("Injury feed JSON has an invalid schema.")
        fetched_raw = str(payload.get("fetched_at_utc") or "").strip()
        if not fetched_raw:
            raise ValueError("Injury feed has not published its first successful report.")
        fetched_dt = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
        if fetched_dt.tzinfo is None:
            fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (datetime.now(timezone.utc) - fetched_dt.astimezone(timezone.utc)).total_seconds())
        stale = age_seconds > 36 * 60 * 60
        lookup, display_rows = {}, []
        for row in payload.get("records") or []:
            if not isinstance(row, dict):
                continue
            player = str(row.get("player_name") or row.get("Player Name") or "").strip()
            team_raw = str(row.get("team") or row.get("Team") or "").strip()
            status = str(row.get("status") or row.get("Current Status") or "").strip()
            if not player or not team_raw or not status:
                continue
            team_code = _canonical_team_code(team_raw)
            player_key = _injury_name_key(player)
            if not team_code or not player_key:
                continue
            normalized = {
                "player": player,
                "team": team_code,
                "status": status,
                "reason": str(row.get("reason") or row.get("Reason") or "").strip(),
                "game_date": str(row.get("game_date") or row.get("Game Date") or "").strip(),
                "game_time": str(row.get("game_time") or row.get("Game Time") or "").strip(),
                "matchup": str(row.get("matchup") or row.get("Matchup") or "").strip(),
            }
            lookup[(player_key, team_code)] = normalized
            display_rows.append(normalized)
        data = {
            "lookup": {} if stale else lookup,
            "records": display_rows,
            "meta": {
                "source": _INJURY_SOURCE_LABEL,
                "status": "stale" if stale else "fresh",
                "report_timestamp_et": payload.get("report_timestamp_et"),
                "fetched_at_utc": fetched_raw,
                "age_minutes": round(age_seconds / 60, 1),
                "record_count": len(display_rows),
                "season": payload.get("season"),
            },
        }
        _INJURY_FEED_CACHE.update({"at": now, "data": data})
        return data
    except Exception as exc:
        if cached is not None:
            return cached
        return {
            "lookup": {},
            "records": [],
            "meta": {"source": _INJURY_SOURCE_LABEL, "status": "unavailable", "record_count": 0,
                     "error": type(exc).__name__},
        }

def _clear_official_injury_fields(prop):
    if prop.get("injury_source") == _INJURY_SOURCE_LABEL:
        for key in ("injury_status", "injury_reason", "injury_report_time", "injury_updated_at",
                    "injury_source", "injury_flag", "injury_blocked", "injury_risk"):
            prop.pop(key, None)

def _apply_official_injury(prop, feed, player_name=None, team_value=None):
    if not isinstance(prop, dict):
        return
    _clear_official_injury_fields(prop)
    if not isinstance(feed, dict) or feed.get("meta", {}).get("status") != "fresh":
        return
    player_key = _injury_name_key(player_name or prop.get("player") or prop.get("player_name"))
    team_code = _canonical_team_code(team_value or prop.get("roster_team") or prop.get("team"))
    if not player_key or not team_code:
        return
    record = feed.get("lookup", {}).get((player_key, team_code))
    if not record:
        return
    status = str(record.get("status") or "").strip()
    status_key = status.casefold()
    blocked = status_key in _INJURY_BLOCK_STATUSES
    prop.update({
        "injury_status": status,
        "injury_reason": record.get("reason") or "",
        "injury_report_time": feed.get("meta", {}).get("report_timestamp_et"),
        "injury_updated_at": feed.get("meta", {}).get("fetched_at_utc"),
        "injury_source": _INJURY_SOURCE_LABEL,
        "injury_flag": blocked,
        "injury_blocked": blocked,
        "injury_risk": status_key not in ("", "available"),
    })
    if blocked:
        prop["recommendation"] = "PASS"
        for key in ("expected_value_pct", "expected_profit_per_100", "edge_pp",
                    "best_side", "best_odds", "best_probability"):
            if key in prop:
                prop[key] = None
        prop["probability_note"] = (
            "No bet: official NBA injury report lists this player as " + status +
            ". Verify the latest availability before betting."
        )


def _espn_player_id(name, timeout=12):
    """Resolve a player to ESPN's numeric NBA athlete ID.

    ESPN global search can return UUID-style content IDs. The common-v3
    gamelog endpoint requires the numeric athlete ID used in ESPN profile
    URLs / athlete UIDs, so never pass an arbitrary search-result UUID.
    """
    key = _norm_player_name(name)
    if key in _ESPN_ID_CACHE:
        return _ESPN_ID_CACHE[key]

    query = urlencode({"query": name, "limit": 20, "sport": "basketball", "league": "nba"})
    payload = _get_json(ESPN_SEARCH + "?" + query, timeout=timeout)
    candidates = []

    def numeric_id(value):
        if value is None:
            return None
        text = str(value).strip()
        return text if text.isdigit() else None

    def id_from_uid(value):
        text = str(value or "")
        # ESPN athlete UIDs commonly contain an a:<numeric athlete id> segment.
        for marker in ("~a:", "a:"):
            pos = text.find(marker)
            if pos >= 0:
                tail = text[pos + len(marker):]
                token = tail.split("~", 1)[0].split(":", 1)[0].split("/", 1)[0]
                found = numeric_id(token)
                if found:
                    return found
        return None

    def id_from_href(value):
        text = str(value or "")
        # ESPN profile URLs commonly contain /id/<numeric-id>/.
        marker = "/id/"
        pos = text.find(marker)
        if pos >= 0:
            tail = text[pos + len(marker):]
            token = tail.split("/", 1)[0].split("?", 1)[0]
            found = numeric_id(token)
            if found:
                return found
        return None

    def walk(value):
        if isinstance(value, dict):
            athlete_obj = value.get("athlete") if isinstance(value.get("athlete"), dict) else value
            candidate_name = (
                athlete_obj.get("fullName") or athlete_obj.get("displayName")
                or athlete_obj.get("name") or value.get("title") or ""
            )
            candidate_id = (
                numeric_id(athlete_obj.get("id"))
                or id_from_uid(athlete_obj.get("uid"))
                or id_from_href(athlete_obj.get("href"))                or id_from_href(value.get("href"))
                or id_from_href(value.get("link"))
            )
            links = athlete_obj.get("links") or value.get("links") or []
            if not candidate_id and isinstance(links, list):
                for link in links:
                    if isinstance(link, dict):
                        candidate_id = id_from_href(link.get("href"))
                        if candidate_id:
                            break
            if candidate_id and candidate_name:
                candidates.append((candidate_id, str(candidate_name)))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    best_id, best_score = None, 0
    for candidate_id, candidate_name in candidates:
        score = SequenceMatcher(None, key, _norm_player_name(candidate_name)).ratio()
        if score > best_score:
            best_id, best_score = candidate_id, score

    if best_score < 0.78:
        best_id = None

    _ESPN_ID_CACHE[key] = best_id
    return best_id

def _gamelog_records(athlete_id, field, label, season):
    """Return stat values with their ESPN event identifiers so stats and minutes align."""
    cache_key = (str(athlete_id), field, season, "records")
    if cache_key in _ESPN_LOG_CACHE:
        return _ESPN_LOG_CACHE[cache_key]
    url = ESPN_BASE + "/athletes/" + str(athlete_id) + "/gamelog?" + urlencode({"season": season})
    payload = _get_json(url)
    if not isinstance(payload, dict):
        _ESPN_LOG_CACHE[cache_key] = []
        return []
    labels = payload.get("labels") or []
    names = payload.get("names") or payload.get("statNames") or []
    default_index = labels.index(label) if isinstance(labels, list) and label in labels else -1
    source_rows = []
    season_types = payload.get("seasonTypes") or []

    def is_regular_season(season_type):
        # ESPN includes preseason, regular-season and postseason categories in
        # the same athlete gamelog response. Only regular-season rows belong in
        # the standard player prop baseline.
        type_info = season_type.get("type") if isinstance(season_type.get("type"), dict) else {}
        ids = {
            str(season_type.get("id") or "").strip().lower(),
            str(type_info.get("id") or "").strip().lower(),
        }
        names_to_check = [
            season_type.get("name"), season_type.get("displayName"),
            season_type.get("abbreviation"), type_info.get("name"),
            type_info.get("displayName"), type_info.get("abbreviation"),
            type_info.get("type"),
        ]
        normalized_names = {
            "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
            for value in names_to_check
        }
        return "2" in ids or any(
            "regularseason" in name and "playin" not in name
            for name in normalized_names
        ) or "reg" in normalized_names

    for season_type in season_types:
        if not isinstance(season_type, dict) or not is_regular_season(season_type):
            continue
        for category in season_type.get("categories") or []:
            if not isinstance(category, dict):
                continue
            cat_labels = category.get("labels") or labels
            cat_names = category.get("names") or names
            idx = cat_labels.index(label) if isinstance(cat_labels, list) and label in cat_labels else -1
            if idx < 0 and isinstance(cat_names, list) and field in cat_names:
                idx = cat_names.index(field)
            events = category.get("events") or []
            if isinstance(events, dict):
                events = list(events.values())
            if isinstance(events, list):
                source_rows.extend((row, idx) for row in events if isinstance(row, dict))
    # Some ESPN response variants expose a flat gameLog instead of seasonTypes.
    # Do not fall back to the flat list when seasonTypes exists: it may contain
    # preseason and postseason games that must not leak into regular-season stats.
    if not source_rows and not season_types:
        direct = payload.get("gameLog") or payload.get("games") or payload.get("events") or []
        if isinstance(direct, dict):
            direct = list(direct.values())
        if isinstance(direct, list):
            source_rows = [(row, default_index) for row in direct if isinstance(row, dict) and isinstance(row.get("stats"), list)]
    records = []
    for row, row_index in source_rows:
        stats = row.get("stats") or []
        if not isinstance(stats, list):
            continue
        idx = row_index if row_index >= 0 else default_index
        if idx < 0 and isinstance(names, list) and len(names) == len(stats) and field in names:
            idx = names.index(field)
        if idx < 0 or idx >= len(stats):
            continue
        raw = str(stats[idx]).replace(",", "").strip()
        if field == "threePointsMade" and "-" in raw:
            raw = raw.split("-", 1)[0].strip()
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        event = row.get("event") if isinstance(row.get("event"), dict) else {}
        event_id = row.get("eventId") or row.get("event_id") or event.get("id") or row.get("id")
        date = row.get("date") or row.get("gameDate") or event.get("date") or ""
        records.append({"event_id": str(event_id) if event_id is not None else None,
                        "date": str(date), "value": value})
    _ESPN_LOG_CACHE[cache_key] = records
    return records


def _gamelog_values(athlete_id, field, label, season):
    """Compatibility helper returning values in the ESPN gamelog order."""
    return [row["value"] for row in _gamelog_records(athlete_id, field, label, season)]


def _player_history(athlete_id, field, label):
    """Align stat and minutes by game ID; use prior season only as limited fallback."""
    current_stats = _gamelog_records(athlete_id, field, label, 2027)
    current_minutes = _gamelog_records(athlete_id, "minutes", "MIN", 2027)
    prior_stats = []
    prior_minutes = []
    # Steals are especially sparse early in the season. When fewer than eight
    # current-season games exist, blend a capped prior-season sample rather
    # than relaxing the EV eligibility threshold or pretending the sample is
    # current-season data. Other markets retain the established <5 fallback.
    prior_trigger = 8 if field == "steals" else 5
    if len(current_stats) < prior_trigger:
        prior_stats = _gamelog_records(athlete_id, field, label, 2026)
        prior_minutes = _gamelog_records(athlete_id, "minutes", "MIN", 2026)
        if field == "steals":
            prior_stats = prior_stats[-8:]
            prior_minutes = prior_minutes[-8:]

    def pair_records(stats, mins):
        minute_by_event = {r["event_id"]: r for r in mins if r.get("event_id")}
        paired = []
        used_minutes = set()
        unmatched_stats = []
        for stat in stats:
            event_id = stat.get("event_id")
            minute = minute_by_event.get(event_id) if event_id else None
            if minute and minute["value"] > 0:
                paired.append((stat, minute))
                used_minutes.add(id(minute))
            else:
                unmatched_stats.append(stat)

        # ESPN can expose different event-ID formats across season/category
        # payloads. Exact game-date matching is a safe secondary key and avoids
        # discarding an otherwise valid prior-season fallback.
        minute_by_date = {}
        for minute in mins:
            date_key = str(minute.get("date") or "")[:10]
            if date_key and minute["value"] > 0:
                minute_by_date.setdefault(date_key, []).append(minute)
        for stat in unmatched_stats:
            date_key = str(stat.get("date") or "")[:10]
            candidates = minute_by_date.get(date_key, []) if date_key else []
            minute = next((item for item in candidates if id(item) not in used_minutes), None)
            if minute:
                paired.append((stat, minute))
                used_minutes.add(id(minute))

        # Positional matching is only safe when neither feed has event IDs and
        # both arrays have identical lengths.
        has_any_event_ids = any(r.get("event_id") for r in stats) or any(r.get("event_id") for r in mins)
        if not paired and not has_any_event_ids and len(stats) == len(mins):
            paired = [(a, b) for a, b in zip(stats, mins) if b["value"] > 0]
        return paired

    paired = pair_records(current_stats, current_minutes)
    used_prior = False
    history_stats = list(current_stats)
    if len(current_stats) < prior_trigger and prior_stats:
        # Prior-season records are a limited prior, not additional current-
        # season evidence. For steals, cap the prior contribution at eight
        # games and keep current-season rows newest in the combined sequence.
        # Minutes remain optional and are joined only by event ID or date.
        history_stats = list(prior_stats) + history_stats
        prior_pairs = pair_records(prior_stats, prior_minutes)
        paired = prior_pairs + paired
        used_prior = True

    history_stats = [
        row for row in history_stats
        if math.isfinite(row.get("value", float("nan"))) and row.get("value", -1) >= 0
    ]
    if any(row.get("date") for row in history_stats):
        history_stats.sort(key=lambda row: row.get("date") or "")
    history_stats = history_stats[-25:]

    # Minutes are used only when verified by event ID or exact game date.
    paired = [(a, b) for a, b in paired if math.isfinite(a["value"]) and math.isfinite(b["value"]) and 0 < b["value"] <= 60]
    if any(a.get("date") for a, _ in paired):
        paired.sort(key=lambda item: item[0].get("date") or "")
    paired = paired[-25:]
    values = [row["value"] for row in history_stats]
    minutes = [b["value"] for _, b in paired]
    sample_label = "2026-27 + 2025-26 limited fallback" if used_prior else "2026-27 regular season"
    return values, sample_label, minutes

def _odds_profit(win_probability, loss_probability, odds):
    if odds is None:
        return None
    try:
        price = float(odds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price == 0:
        return None
    payout = 100.0 / abs(price) if price < 0 else price / 100.0
    return win_probability * payout - loss_probability

def _median(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _projected_minutes(valid_minutes):
    """Blend recent and established playing time so one short stretch cannot dominate."""
    clean = []
    for item in valid_minutes or []:
        try:
            value = float(item)
            if math.isfinite(value) and 0 < value <= 60:
                clean.append(value)
        except (TypeError, ValueError):
            continue
    if not clean:
        return None
    recent5 = _median(clean[-5:])
    recent10 = _median(clean[-10:])
    established = _median(clean[-20:])
    if len(clean) < 5:
        return recent5
    # Give recent role changes weight without allowing a handful of games to
    # erase the player's established rotation baseline.
    return 0.55 * recent5 + 0.30 * recent10 + 0.15 * established

def _simulate_market(values, line, over_odds, under_odds, seed, minutes=None, market="", matchup_adjustment=None):
    """Project count stats with an overdispersed count model; never truncate the upper tail at the observed maximum."""
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v) >= 0]
    if len(clean) < 4:
        return None
    n = len(clean)
    recent5, recent10 = clean[-5:], clean[-10:]
    long_mean = sum(clean) / n
    recent5_mean = sum(recent5) / len(recent5)
    recent10_mean = sum(recent10) / len(recent10)
    weights = [math.exp(-0.075 * (len(clean)-1-i)) for i in range(len(clean))]
    weighted_mean = sum(v*w for v,w in zip(clean,weights)) / sum(weights)
    form_weight = min(0.30, len(recent10)/(len(recent10)+20.0))
    target_mean = (1.0-form_weight)*long_mean + form_weight*weighted_mean

    valid_minutes = []
    for item in (minutes or []):
        try:
            value = float(item)
            if math.isfinite(value) and value > 0:
                valid_minutes.append(value)
        except (TypeError, ValueError):
            continue
    projected_min = _projected_minutes(valid_minutes)
    baseline_min = _median(valid_minutes[-20:]) if valid_minutes else None
    minute_factor = 1.0
    if projected_min is not None and baseline_min and baseline_min > 0 and len(valid_minutes) >= 8:
        minute_factor = max(0.85, min(1.15, projected_min / baseline_min))
        target_mean *= minute_factor

    matchup_factor = 1.0
    if isinstance(matchup_adjustment, dict) and matchup_adjustment.get("available", True):
        try:
            candidate = float(matchup_adjustment.get("factor", 1.0))
            if math.isfinite(candidate):
                matchup_factor = max(0.92, min(1.08, candidate))
                target_mean *= matchup_factor
        except (TypeError, ValueError):
            matchup_factor = 1.0

    target_mean = max(0.05, target_mean)
    distribution = [max(0.0, v * (target_mean / long_mean)) if long_mean > 0 else v for v in clean]
    empirical_variance = sum((v - sum(distribution)/len(distribution))**2 for v in distribution) / max(1, n-1)

    # NBA box-score stats are counts. An eight-game sample can have a tiny
    # observed variance and no outcomes above the historical maximum. That is
    # not evidence that the true upper tail is nearly impossible. Use a
    # Gamma-Poisson (negative-binomial) distribution with at least Poisson
    # variance, and shrink noisy dispersion estimates toward a conservative
    # prior until a larger sample is available.
    excess_variance = max(0.0, empirical_variance - target_mean)
    raw_dispersion = min(30.0, max(4.0, target_mean**2 / excess_variance)) if excess_variance > 1e-9 else 30.0
    prior_dispersion = 15.0
    dispersion_k = max(4.0, min(30.0, (n/(n+12.0))*raw_dispersion + (12.0/(n+12.0))*prior_dispersion))
    model_variance = target_mean + (target_mean**2 / dispersion_k)
    std_dev = math.sqrt(model_variance)

    rng = random.Random(str(seed) + "|" + str(line) + "|" + str(market))
    sims = 60000
    over = under = push = 0
    empirical_weight = 0.20

    def poisson_draw(lam):
        # Knuth sampler; NBA player-stat rates are comfortably below 30.
        threshold = math.exp(-lam)
        product = 1.0
        count = 0
        while product > threshold:
            count += 1
            product *= rng.random()
        return count - 1

    for _ in range(sims):
        if rng.random() < empirical_weight:
            sample = distribution[rng.randrange(len(distribution))]
        else:
            latent_rate = rng.gammavariate(dispersion_k, target_mean / dispersion_k)
            sample = poisson_draw(latent_rate)
        if abs(sample - line) < 1e-9:
            push += 1
        elif sample > line:
            over += 1
        else:
            under += 1

    over_p, under_p, push_p = over/sims, under/sims, push/sims
    over_ev = _odds_profit(over_p, under_p, over_odds)
    under_ev = _odds_profit(under_p, over_p, under_odds)
    best = max([(v, side, prob, price) for v, side, prob, price in [
        (over_ev, "OVER", over_p, over_odds), (under_ev, "UNDER", under_p, under_odds)
    ] if v is not None], key=lambda x: x[0], default=None)
    return {
        "projection": round(target_mean, 2),
        "projected_minutes": round(projected_min, 1) if projected_min is not None else None,
        "over_probability": round(over_p*100, 2), "under_probability": round(under_p*100, 2),
        "push_probability": round(push_p*100, 2), "simulations": sims,
        "best_side": best[1] if best else None,
        "best_probability": round(best[2]*100, 2) if best else None,
        "best_odds": best[3] if best else None,
        "expected_value_pct": round(best[0]*100, 2) if best else None,
        "edge_pp": None,
        "recommendation": "VALUE CANDIDATE" if best and best[0] > 0.05 else ("LEAN / VERIFY" if best and best[0] > 0 else "PASS"),
        "model_components": ["recency-weighted history", "10-game form signal", "minutes adjustment" if valid_minutes else "minutes unavailable; no adjustment", "defense-vs-position matchup adjustment" if isinstance(matchup_adjustment, dict) and matchup_adjustment.get("available", True) else "defense-vs-position unavailable; no adjustment", "20% empirical history + 80% negative-binomial count distribution"],
        "model_method": "recency-weighted count-rate projection with Gamma-Poisson overdispersion; provisional pending out-of-sample calibration",
        "sample_size": n, "long_run_mean": round(long_mean, 2), "recent5_mean": round(recent5_mean, 2),
        "recent10_mean": round(recent10_mean, 2), "weighted_mean": round(weighted_mean, 2),
        "distribution_std_dev": round(std_dev, 2), "minute_adjustment_factor": round(minute_factor, 3),
        "minutes_sample_size": len(valid_minutes),
        "matchup_adjustment_pct": round((matchup_factor - 1.0) * 100, 2),
        "defense_vs_position": matchup_adjustment,
        "baseline_minutes": round(baseline_min, 1) if baseline_min is not None else None,
        "minutes_method": "55% recent-5 median / 30% recent-10 median / 15% recent-20 median; adjustment capped at +/-15%",
        "count_dispersion_k": round(dispersion_k, 2),
        "calibration_status": "uncalibrated"
    }

_BP_OFFERS_CACHE = {}
_BP_BOOKS_CACHE = {"at": 0, "ids": {}}
_BP_PROPS_CACHE = {}
_BP_BOARD_CACHE = {}
_BP_RATE_LIMIT_UNTIL = 0.0
_BP_MARKET_RATE_LIMIT_UNTIL = {}
_BP_CACHE_TTL_SECONDS = 300
_BP_BOARD_TTL_SECONDS = 180
_BP_RATE_LIMIT_COOLDOWN_SECONDS = 300
_BP_STALE_CACHE_SECONDS = 3600
_BP_EVENT_MATCHUP_CACHE = {}

def _bp_event_matchup(event_id):
    """Resolve BettingPros event IDs to an away-at-home matchup for DVP."""
    key=str(event_id or "").strip()
    if not key:
        return ""
    cached=_BP_EVENT_MATCHUP_CACHE.get(key)
    if cached and time.time()-cached["at"]<3600:
        return cached["matchup"]
    api_key=os.getenv("BETTINGPROS_API_KEY","").strip()
    if not api_key:
        return ""
    headers={"x-api-key":api_key,"accept":"application/json","user-agent":"ColtsNBAEdge/1.0"}
    urls=[
        "https://api.bettingpros.com/v3/events?"+urlencode({"sport":"NBA","event_id":key}),
        "https://api.bettingpros.com/v3/events/"+key+"?"+urlencode({"sport":"NBA"}),
    ]
    payload=None
    for url in urls:
        try:
            req=Request(url,headers=headers)
            with urlopen(req,timeout=10) as response:
                candidate=json.loads(response.read().decode("utf-8","replace"))
            if isinstance(candidate,dict):
                payload=candidate
                break
        except Exception:
            continue
    matchup=""
    def walk(value):
        if isinstance(value,dict):
            home=value.get("home_team") or value.get("homeTeam") or value.get("home")
            away=value.get("away_team") or value.get("awayTeam") or value.get("away")
            if isinstance(home,dict): home=home.get("abbreviation") or home.get("short_name") or home.get("name") or home.get("display_name")
            if isinstance(away,dict): away=away.get("abbreviation") or away.get("short_name") or away.get("name") or away.get("display_name")
            if home and away:
                return str(away)+" @ "+str(home)
            for child in value.values():
                found=walk(child)
                if found: return found
        elif isinstance(value,list):
            for child in value:
                found=walk(child)
                if found: return found
        return ""
    if payload:
        matchup=walk(payload)
    _BP_EVENT_MATCHUP_CACHE[key]={"at":time.time(),"matchup":matchup}
    return matchup

def _bp_fetch_props(market, limit=50):
    """Shared per-market cache; never fan out repeated props requests during a provider cooldown."""
    now = time.time()
    cached = _BP_PROPS_CACHE.get(market)
    if cached and now - cached[0] < _BP_CACHE_TTL_SECONDS:
        return cached[1], "cache"
    market_cooldown_until = _BP_MARKET_RATE_LIMIT_UNTIL.get(market, 0.0)
    if now < market_cooldown_until:
        if cached and now - cached[0] < _BP_STALE_CACHE_SECONDS:
            return cached[1], "stale-cache"
        retry = max(1, int(market_cooldown_until - now))
        raise HTTPException(status_code=503, detail="BettingPros cooldown active. Retry in " + str(retry) + " seconds.")
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BETTINGPROS_API_KEY is missing.")
    url = "https://api.bettingpros.com/v3/props?" + urlencode({
        "sport": "NBA", "market_id": BP_MARKETS[market], "page": 1, "limit": limit
    })
    req = Request(url, headers={"x-api-key": api_key, "accept": "application/json",
                                "user-agent": "ColtsNBAEdge/1.0"})
    try:
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid BettingPros response.")
        _BP_PROPS_CACHE[market] = (time.time(), payload)
        return payload, "live"
    except HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                cooldown = max(_BP_RATE_LIMIT_COOLDOWN_SECONDS, min(1800, int(retry_after)))
            except (TypeError, ValueError):
                cooldown = _BP_RATE_LIMIT_COOLDOWN_SECONDS
            _BP_MARKET_RATE_LIMIT_UNTIL[market] = time.time() + cooldown
            if cached and time.time() - cached[0] < _BP_STALE_CACHE_SECONDS:
                return cached[1], "stale-cache"
            raise HTTPException(status_code=503, detail="BettingPros rate limit reached. Cooling down for " + str(cooldown) + " seconds.") from exc
        if exc.code in (401, 403):
            raise HTTPException(status_code=502, detail="BettingPros rejected the API key or this account tier does not allow this endpoint.") from exc
        raise HTTPException(status_code=502, detail="BettingPros props endpoint returned HTTP " + str(exc.code) + ".") from exc
    except HTTPException:
        raise
    except Exception as exc:
        if cached and time.time() - cached[0] < _BP_STALE_CACHE_SECONDS:
            return cached[1], "stale-cache"
        raise HTTPException(status_code=502, detail="Could not retrieve " + market + " prop lines from BettingPros.") from exc

def _american_probability(odds):
    try:
        value = float(odds)
        if not math.isfinite(value) or value == 0:
            return None
        return abs(value) / (abs(value) + 100.0) if value < 0 else 100.0 / (value + 100.0)
    except (TypeError, ValueError):
        return None

def _american_payout(odds):
    value = float(odds)
    return 100.0 / abs(value) if value < 0 else value / 100.0

def _bp_books():
    now = __import__("time").time()
    if now - _BP_BOOKS_CACHE["at"] < 3600 and _BP_BOOKS_CACHE["ids"]:
        return _BP_BOOKS_CACHE["ids"]
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        return {}
    req = Request("https://api.bettingpros.com/v3/books?sport=NBA",
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"})
    try:
        with urlopen(req, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        rows = payload.get("books", []) if isinstance(payload, dict) else []
        ids = {}
        for row in rows:
            name = str(row.get("name") or row.get("title") or "").lower()
            if "draftkings" in name or "draft kings" in name:
                ids["draftkings"] = int(row["id"])
            elif "fanduel" in name or "fan duel" in name:
                ids["fanduel"] = int(row["id"])
        _BP_BOOKS_CACHE.update({"at": now, "ids": ids})
        return ids
    except Exception:
        return {}

def _bp_event_offers(event_id, market):
    global _BP_RATE_LIMIT_UNTIL
    key = (str(event_id), market)
    now = time.time()
    cached = _BP_OFFERS_CACHE.get(key)
    if cached and now - cached[0] < 600:
        return cached[1]
    if now < _BP_RATE_LIMIT_UNTIL:
        return []
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        return []
    params = urlencode({"sport": "NBA", "market_id": BP_MARKETS[market], "event_id": event_id, "page": 1, "limit": 50})
    req = Request("https://api.bettingpros.com/v3/offers?" + params,
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"})
    try:
        with urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        rows = payload.get("offers", []) if isinstance(payload, dict) else []
        _BP_OFFERS_CACHE[key] = (now, rows)
        return rows
    except HTTPError as exc:
        if exc.code == 429:
            _BP_RATE_LIMIT_UNTIL = time.time() + _BP_RATE_LIMIT_COOLDOWN_SECONDS
        return []
    except Exception:
        return []

def _bp_two_book_market(offers, player_name):
    """Return a same-line DK/FD no-vig blend and the best price at that exact line."""
    books = _bp_books()
    wanted = {books.get("draftkings"), books.get("fanduel")} - {None}
    if not wanted:
        return None
    target = _norm_player_name(player_name)
    matches = []
    for offer in offers:
        participants = offer.get("participants") or []
        names = [_norm_player_name(p.get("name") or "") for p in participants if isinstance(p, dict)]
        if not names or max((SequenceMatcher(None, target, n).ratio() for n in names), default=0) < 0.90:
            continue
        side_prices = {"over": {}, "under": {}}
        for selection in offer.get("selections") or []:
            side = str(selection.get("selection") or "").lower()
            if side not in side_prices:
                continue
            for book in selection.get("books") or []:
                try:
                    book_id = int(book.get("id"))
                except (TypeError, ValueError):
                    continue
                if book_id not in wanted:
                    continue
                for line_obj in book.get("lines") or []:
                    if not line_obj.get("active", True) or line_obj.get("is_off"):
                        continue
                    try:
                        line = float(line_obj.get("line"))
                        odds = float(line_obj.get("cost"))
                    except (TypeError, ValueError):
                        continue
                    if odds == 0:
                        continue
                    side_prices[side].setdefault(line, {})[book_id] = {
                        "odds": odds, "link": line_obj.get("link"), "updated": line_obj.get("updated")
                    }
        common = set(side_prices["over"]) & set(side_prices["under"])
        candidates = []
        for line in common:
            covered = [bid for bid in wanted if bid in side_prices["over"][line] and bid in side_prices["under"][line]]
            if covered:
                candidates.append((len(covered), line, covered))
        if candidates:
            _, line, covered = max(candidates, key=lambda x: (x[0], -x[1]))
            pairs = []
            for bid in covered:
                over = side_prices["over"][line][bid]
                under = side_prices["under"][line][bid]
                qo, qu = _american_probability(over["odds"]), _american_probability(under["odds"])
                if qo is None or qu is None or qo + qu <= 0:
                    continue
                pairs.append({"book_id": bid, "over_probability": qo/(qo+qu),
                              "under_probability": qu/(qo+qu), "over": over, "under": under})
            if pairs:
                matches.append((len(pairs), line, pairs))
    if not matches:
        return None
    _, line, pairs = max(matches, key=lambda x: (x[0], len(x[2])))
    over_prob = sum(x["over_probability"] for x in pairs) / len(pairs)
    under_prob = sum(x["under_probability"] for x in pairs) / len(pairs)
    over_best = max((x["over"] for x in pairs), key=lambda x: _american_payout(x["odds"]))
    under_best = max((x["under"] for x in pairs), key=lambda x: _american_payout(x["odds"]))
    names = {v: k for k, v in books.items()}
    return {"line": line, "over_probability": over_prob, "under_probability": under_prob,
            "over_odds": over_best["odds"], "under_odds": under_best["odds"],
            "over_book": names.get(next(x["book_id"] for x in pairs if x["over"] is over_best), "sportsbook"),
            "under_book": names.get(next(x["book_id"] for x in pairs if x["under"] is under_best), "sportsbook"),
            "books_used": [names.get(x["book_id"], str(x["book_id"])) for x in pairs]}

def _log_pool(model_p, market_p, weight=0.5):
    eps = 1e-6
    a = max(eps, min(1-eps, float(model_p)))
    b = max(eps, min(1-eps, float(market_p)))
    z = math.exp((1-weight)*math.log(a) + weight*math.log(b))
    other = math.exp((1-weight)*math.log(1-a) + weight*math.log(1-b))
    return z / (z + other)



_CALIBRATION_PROFILE_CACHE = {"at": 0.0, "data": {}}
_CALIBRATION_PROFILE_TTL = 300


def _calibration_profile(market):
    now = time.time()
    cached = _CALIBRATION_PROFILE_CACHE.get("data") or {}
    if now - _CALIBRATION_PROFILE_CACHE.get("at", 0.0) < _CALIBRATION_PROFILE_TTL and market in cached:
        return cached[market]
    profile = prediction_ledger.calibration_profile(market)
    cached[market] = profile
    _CALIBRATION_PROFILE_CACHE.update({"at": now, "data": cached})
    return profile


def _apply_historical_calibration(model, market):
    """Apply a market-specific empirical reliability correction only when evidence is adequate."""
    profile = _calibration_profile(market)
    model["calibration_sample_size"] = profile.get("sample_size", 0)
    model["calibration_bins"] = profile.get("bins", [])
    if not profile.get("ready"):
        model["calibration_status"] = "collecting data"
        model["calibration_applied"] = False
        model["calibration_minimum_sample_size"] = profile.get("minimum_sample_size", 100)
        return model
    push = max(0.0, min(0.25, float(model.get("push_probability") or 0.0) / 100.0))
    mass = max(1e-9, 1.0 - push)
    raw_over = max(0.0, min(1.0, float(model.get("over_probability") or 0.0) / 100.0 / mass))
    bins = profile.get("bins") or []
    eligible = [b for b in bins if b.get("count", 0) >= 20]
    if len(eligible) < 3:
        model["calibration_status"] = "collecting data"
        model["calibration_applied"] = False
        model["calibration_minimum_populated_bins"] = 3
        return model
    # Weighted pool-adjacent-violators keeps the learned mapping monotonic.
    blocks = []
    for b in sorted(eligible, key=lambda item: float(item["predicted"])):
        blocks.append({
            "x_sum": float(b["predicted"]) * int(b["count"]),
            "y_sum": float(b["observed"]) * int(b["count"]),
            "weight": int(b["count"]),
        })
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            if left["y_sum"] / left["weight"] <= right["y_sum"] / right["weight"]:
                break
            merged = {
                "x_sum": left["x_sum"] + right["x_sum"],
                "y_sum": left["y_sum"] + right["y_sum"],
                "weight": left["weight"] + right["weight"],
            }
            blocks[-2:] = [merged]
    centers = [(b["x_sum"] / b["weight"], b["y_sum"] / b["weight"]) for b in blocks]
    if raw_over <= centers[0][0]:
        calibrated_over = centers[0][1]
    elif raw_over >= centers[-1][0]:
        calibrated_over = centers[-1][1]
    else:
        calibrated_over = raw_over
        for (x0, y0), (x1, y1) in zip(centers, centers[1:]):
            if x0 <= raw_over <= x1:
                weight = (raw_over - x0) / max(1e-9, x1 - x0)
                calibrated_over = y0 + weight * (y1 - y0)
                break
    calibrated_over = max(0.01, min(0.99, calibrated_over))
    model["raw_over_probability"] = model.get("over_probability")
    model["raw_under_probability"] = model.get("under_probability")
    model["raw_model_over_probability"] = model.get("over_probability")
    model["raw_model_under_probability"] = model.get("under_probability")
    model["over_probability"] = round(calibrated_over * mass * 100, 2)
    model["under_probability"] = round((1.0 - calibrated_over) * mass * 100, 2)
    model["calibration_status"] = "historically calibrated"
    model["calibration_applied"] = True
    model["calibration_method"] = "market-specific side-normalized empirical reliability map"
    return model

def _score_blended_market(model, market_quote):
    """Keep the proprietary probability independent; use the book only to price and measure edge."""
    model_over = max(0.0, min(1.0, model["over_probability"] / 100.0))
    model_under = max(0.0, min(1.0, model["under_probability"] / 100.0))
    model_push = max(0.0, min(0.25, model.get("push_probability", 0.0) / 100.0))
    model_mass = max(1e-9, model_over + model_under)
    model_over_cond = model_over / model_mass
    model_under_cond = model_under / model_mass
    market_over = market_quote["over_probability"]
    market_under = market_quote["under_probability"]
    market_mass = max(1e-9, market_over + market_under)
    market_over_cond = market_over / market_mass
    market_under_cond = market_under / market_mass

    # No fixed 50/50 opinion pool: the proprietary model must stand on its own.
    # Sportsbook prices are used only for expected value and market-relative edge.
    over = model_over
    under = model_under
    choices = []
    for side, prob, conditional_prob, market_prob, loss, odds in (
        ("OVER", over, model_over_cond, market_over_cond, under, market_quote["over_odds"]),
        ("UNDER", under, model_under_cond, market_under_cond, over, market_quote["under_odds"]),
    ):
        ev = _odds_profit(prob, loss, odds)
        if ev is not None:
            choices.append((ev, side, prob, odds, loss, conditional_prob, market_prob))
    best = max(choices, key=lambda x: x[0], default=None)
    over_ev = next((item[0] for item in choices if item[1] == "OVER"), None)
    under_ev = next((item[0] for item in choices if item[1] == "UNDER"), None)
    over_edge = (model_over_cond - market_over_cond) * 100
    under_edge = (model_under_cond - market_under_cond) * 100
    model.update({
        "over_ev_pct": round(over_ev * 100, 2) if over_ev is not None else None,
        "under_ev_pct": round(under_ev * 100, 2) if under_ev is not None else None,
        "over_edge_pp": round(over_edge, 2),
        "under_edge_pp": round(under_edge, 2),
        "selection_reason": (
            "Selected " + best[1] + " because it has the higher expected value at the listed odds; "
            "the projection is an average, not the selected betting side."
        ) if best else "No side has a calculable expected value at the listed odds.",
        "model_over_probability": round(model_over * 100, 2),
        "model_under_probability": round(model_under * 100, 2),
        "market_over_probability": round(market_over_cond * 100, 2),
        "market_under_probability": round(market_under_cond * 100, 2),
        "over_probability": round(over * 100, 2),
        "under_probability": round(under * 100, 2),
        "push_probability": round(model_push * 100, 2),
        "best_side": best[1] if best else None,
        "best_probability": round(best[2] * 100, 2) if best else None,
        "best_odds": best[3] if best else None,
        "expected_value_pct": round(best[0] * 100, 2) if best else None,
        "edge_pp": round((best[5] - best[6]) * 100, 2) if best else None,
        "recommendation": "VALUE CANDIDATE" if best and best[0] > 0.05 else ("LEAN / VERIFY" if best and best[0] > 0 else "PASS"),
        "kelly_fraction_pct": round(max(0.0, (best[2] * _american_payout(best[3]) - best[4]) /
            max(1e-9, _american_payout(best[3]) * (best[2] + best[4]))) * 100, 2) if best else None,
        "blend_method": "No probability blend; independent proprietary probability priced against DraftKings/FanDuel odds",
        "market_weight": 0.0,
        "market_books": market_quote["books_used"],
        "best_over_book": market_quote["over_book"],
        "best_under_book": market_quote["under_book"],
        "calibration_status": model.get("calibration_status", "collecting data"),
        "calibration_applied": bool(model.get("calibration_applied", False)),
    })
    return model

_PROPLINE_CACHE = {"at": 0.0, "events": None, "headers": {}}
_PROPLINE_CACHE_TTL = 60
_PROPLINE_STALE_TTL = 600
_PROPLINE_COOLDOWN_UNTIL = 0.0
_PROPLINE_MARKETS = {
    "points": "player_points",
    "rebounds": "player_rebounds",
    "assists": "player_assists",
    "steals": "player_steals",
    "threes": "player_threes",
}


def _propline_fetch_events():
    """Fetch one cached DraftKings-only NBA slate from PropLine."""
    global _PROPLINE_COOLDOWN_UNTIL
    now = time.time()
    cached_events = _PROPLINE_CACHE.get("events")
    age = now - float(_PROPLINE_CACHE.get("at") or 0.0)
    if cached_events is not None and age < _PROPLINE_CACHE_TTL:
        return cached_events, "cache", _PROPLINE_CACHE.get("headers") or {}

    api_key = os.getenv("PROPLINE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="PROPLINE_API_KEY is missing from this deployment.")

    if now < _PROPLINE_COOLDOWN_UNTIL:
        if cached_events is not None and age < _PROPLINE_STALE_TTL:
            return cached_events, "stale-cache", _PROPLINE_CACHE.get("headers") or {}
        retry = max(1, int(_PROPLINE_COOLDOWN_UNTIL - now))
        raise HTTPException(status_code=503, detail="PropLine rate-limit cooldown active. Retry in " + str(retry) + " seconds.")

    params = urlencode({
        "markets": ",".join(_PROPLINE_MARKETS.values()),
        "bookmakers": "draftkings,fanduel",
        "includeLinks": "true",
        "max_age": 300,
    })
    request = Request(
        "https://api.prop-line.com/v1/sports/basketball_nba/odds?" + params,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "ColtsNBAEdge/1.0",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
            headers = {
                "daily_limit": response.headers.get("X-Daily-Limit"),
                "daily_used": response.headers.get("X-Daily-Used"),
                "daily_remaining": response.headers.get("X-Daily-Remaining"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        if isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            events = payload.get("events") or payload.get("data") or payload.get("results")
            if not isinstance(events, list):
                events = [payload] if payload.get("bookmakers") else []
        else:
            events = []
        if not isinstance(events, list):
            raise ValueError("PropLine odds response did not contain an event list.")
        _PROPLINE_CACHE.update({"at": time.time(), "events": events, "headers": headers})
        _PROPLINE_COOLDOWN_UNTIL = 0.0
        return events, "live", headers
    except HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                cooldown = max(1, min(3600, int(retry_after)))
            except (TypeError, ValueError):
                cooldown = 60
            _PROPLINE_COOLDOWN_UNTIL = time.time() + cooldown
            if cached_events is not None and age < _PROPLINE_STALE_TTL:
                return cached_events, "stale-cache", _PROPLINE_CACHE.get("headers") or {}
            raise HTTPException(status_code=503, detail="PropLine request limit reached. Retry in " + str(cooldown) + " seconds.") from exc
        if exc.code in (401, 403):
            raise HTTPException(status_code=502, detail="PropLine rejected PROPLINE_API_KEY or this account tier.") from exc
        raise HTTPException(status_code=502, detail="PropLine NBA odds endpoint returned HTTP " + str(exc.code) + ".") from exc
    except HTTPException:
        raise
    except Exception as exc:
        if cached_events is not None and age < _PROPLINE_STALE_TTL:
            return cached_events, "stale-cache", _PROPLINE_CACHE.get("headers") or {}
        raise HTTPException(status_code=502, detail="Could not retrieve DraftKings NBA props from PropLine: " + type(exc).__name__ + ".") from exc


def _propline_market_props(events, market):
    """Normalize DraftKings main-line Over/Under outcomes into one row per player."""
    market_key = _PROPLINE_MARKETS[market]
    grouped = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "")
        away = str(event.get("away_team") or "")
        home = str(event.get("home_team") or "")
        matchup = (away + " @ " + home).strip(" @")
        for bookmaker in event.get("bookmakers") or []:
            if not isinstance(bookmaker, dict):
                continue
            bookmaker_key = str(bookmaker.get("key") or "").lower()
            if bookmaker_key not in ("draftkings", "fanduel"):
                continue
            for market_row in bookmaker.get("markets") or []:
                if not isinstance(market_row, dict) or market_row.get("key") != market_key:
                    continue
                if market_row.get("suspended_at"):
                    continue
                line_type = str(market_row.get("line_type") or "").lower()
                if line_type and line_type != "main":
                    continue
                updated = market_row.get("last_update")
                for outcome in market_row.get("outcomes") or []:
                    if not isinstance(outcome, dict):
                        continue
                    side = str(outcome.get("name") or "").strip().lower()
                    if side not in ("over", "under"):
                        continue
                    player_text = str(
                        outcome.get("description")
                        or outcome.get("player")
                        or market_row.get("player")
                        or ""
                    ).strip()
                    if not player_text:
                        continue
                    team = ""
                    team_match = re.search(r"\(([A-Z]{2,3})\)\s*$", player_text)
                    if team_match:
                        team = _canonical_team_code(team_match.group(1)) or team_match.group(1)
                        player_text = player_text[:team_match.start()].strip()
                    try:
                        point = float(outcome.get("point"))
                        price = int(float(outcome.get("price")))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not math.isfinite(point) or price == 0 or abs(price) > 10000:
                        continue
                    key = (event_id, _norm_player_name(player_text), point)
                    row = grouped.setdefault(key, {
                        "player": player_text,
                        "team": team,
                        "game": matchup,
                        "event_id": event_id,
                        "line": point,
                        "over_odds": None,
                        "under_odds": None,
                        "last_update": updated,
                        "bookmaker": bookmaker.get("title") or bookmaker_key.title(),
                        "bookmaker_link": bookmaker.get("link"),
                        "book_prices": {"over": {}, "under": {}},
                        "book_names": {},
                        "book_links": {},
                    })
                    row["book_prices"][side][bookmaker_key] = price
                    row["book_names"][bookmaker_key] = bookmaker.get("title") or bookmaker_key.title()
                    if bookmaker.get("link"):
                        row["book_links"][bookmaker_key] = bookmaker.get("link")
    output = []
    for row in grouped.values():
        for side in ("over", "under"):
            prices = row.get("book_prices", {}).get(side, {})
            if prices:
                best_key, best_price = max(prices.items(), key=lambda item: float(item[1]) if float(item[1]) > 0 else 100.0 / abs(float(item[1])))
                row[side + "_odds"] = best_price
                row["best_" + side + "_book"] = row.get("book_names", {}).get(best_key, best_key.title())
        row["market_books"] = sorted(set(row.get("book_names", {}).values()))
        selected_book = None
        for side in ("over", "under"):
            name = row.get("best_" + side + "_book")
            if name:
                selected_book = next((key for key, value in row.get("book_names", {}).items() if value == name), selected_book)
        if selected_book and row.get("book_links", {}).get(selected_book):
            row["bookmaker_link"] = row["book_links"][selected_book]
        if row.get("over_odds") is None or row.get("under_odds") is None:
            continue
        output.append(row)
    output.sort(key=lambda row: (row.get("game") or "", row.get("player") or ""))
    return output


def _propline_quote(prop):
    over_odds = prop.get("over_odds")
    under_odds = prop.get("under_odds")
    over_probability = _american_probability(over_odds)
    under_probability = _american_probability(under_odds)
    if over_probability is None or under_probability is None:
        return None
    total = over_probability + under_probability
    if total <= 0:
        return None
    return {
        "over_probability": over_probability / total,
        "under_probability": under_probability / total,
        "over_odds": over_odds,
        "under_odds": under_odds,
        "over_book": prop.get("best_over_book") or "Sportsbook",
        "under_book": prop.get("best_under_book") or "Sportsbook",
        "books_used": prop.get("market_books") or ["Sportsbook"],
    }


def _active_nba_slate_date(now_et=None):
    """Keep the prior night's slate active until 7:00 a.m. Eastern."""
    now_et = now_et or datetime.now(ZoneInfo("America/New_York"))
    active_date = now_et.date() - timedelta(days=1) if now_et.hour < 7 else now_et.date()
    return active_date.isoformat()


def _post_selection_negative_review(prop, market, injury_feed):
    """Run expensive injury/workload checks only after a prop reaches the provisional Top 5."""
    if not isinstance(prop, dict):
        return False, "Invalid candidate"
    _apply_official_injury(prop, injury_feed, prop.get("player"), prop.get("team"))
    if prop.get("injury_blocked") or prop.get("injury_flag"):
        prop["negative_review"] = {
            "status": "blocked",
            "reason": "Official injury report status: " + str(prop.get("injury_status") or "unavailable"),
        }
        return False, prop["negative_review"]["reason"]

    athlete_id = str(prop.get("athlete_id") or "").strip()
    if not athlete_id:
        prop["negative_review"] = {"status": "inconclusive", "reason": "No verified athlete ID"}
        return False, prop["negative_review"]["reason"]

    field, label = STAT_FIELDS[market]
    current_stats = _gamelog_records(athlete_id, field, label, 2027)
    current_minutes_records = _gamelog_records(athlete_id, "minutes", "MIN", 2027)
    prior_minutes_records = _gamelog_records(athlete_id, "minutes", "MIN", 2026)

    def clean_minutes(rows):
        values = []
        for row in rows or []:
            try:
                value = float(row.get("value"))
                if math.isfinite(value) and 0 < value <= 60:
                    values.append(value)
            except (TypeError, ValueError, AttributeError):
                continue
        return values

    current_minutes_records = sorted(current_minutes_records or [], key=lambda row: str(row.get("date") or ""))
    prior_minutes_records = sorted(prior_minutes_records or [], key=lambda row: str(row.get("date") or ""))
    current_minutes = clean_minutes(current_minutes_records)
    prior_minutes = clean_minutes(prior_minutes_records)
    recent_minutes = current_minutes[-3:]
    recent_median = _median(recent_minutes)
    prior_baseline = _median(prior_minutes[-10:])
    current_games = len(current_stats)
    # Detect a return from a meaningful absence even when the official report
    # has already changed the player to Available. ESPN game logs omit DNP rows,
    # so the date gap between consecutive appearances is the reliable signal.
    played_dates = sorted({
        str(row.get("date") or "")[:10]
        for row in current_stats
        if str(row.get("date") or "")[:10]
    })
    return_gap_days = 0
    if len(played_dates) >= 2:
        try:
            last_played = datetime.strptime(played_dates[-1], "%Y-%m-%d").date()
            previous_played = datetime.strptime(played_dates[-2], "%Y-%m-%d").date()
            return_gap_days = (last_played - previous_played).days
        except (TypeError, ValueError):
            return_gap_days = 0
    return_after_absence = current_games <= 8 and return_gap_days >= 7
    official_status = str(prop.get("injury_status") or "").strip()
    injury_reason = str(prop.get("injury_reason") or "").strip()
    injury_text = (official_status + " " + injury_reason).casefold()
    injury_context = any(token in injury_text for token in (
        "calf", "hamstring", "ankle", "knee", "hip", "back", "foot",
        "groin", "thigh", "quad", "wrist", "shoulder", "illness",
        "minutes restriction", "management", "injury"
    ))
    return_window = current_games <= 8 and prior_baseline is not None and recent_median is not None
    workload_ratio = (recent_median / prior_baseline) if prior_baseline and recent_median is not None else 1.0
    role_drop = prior_baseline is not None and recent_median is not None and workload_ratio < 0.88
    recent_return = return_after_absence and return_window
    reported_injury = bool(injury_context and injury_reason and official_status)

    if return_after_absence and (prior_baseline is None or recent_median is None):
        prop["negative_review"] = {
            "status": "inconclusive",
            "reason": "Recent return after a 7+ day gap, but verified current minutes or prior workload baseline is missing; no bet until workload is confirmed",
            "return_gap_days": return_gap_days,
        }
        return False, prop["negative_review"]["reason"]

    if current_games == 0:
        prop["negative_review"] = {
            "status": "inconclusive",
            "reason": "No current-season game log is available to verify present workload",
        }
        return False, prop["negative_review"]["reason"]

    # A recent return or material minutes drop triggers a second, workload-adjusted
    # probability calculation. The sportsbook price is never used to form the mean.
    if recent_return or role_drop or reported_injury:
        # A return after a 7+ day absence is capped at 85% of the
        # established rate, and reduced further when observed minutes show
        # the player is still below his normal workload.
        factor = max(0.40, min(0.85, workload_ratio if (role_drop or recent_return) else 0.85))
        prop["negative_review"] = {"status": "reviewing", "reason": "Recent workload or injury signal detected"}
        values, sample_label, _ = _player_history(athlete_id, field, label)
        adjusted_values = [max(0.0, float(value) * factor) for value in values]
        over_odds = prop.get("over_odds")
        under_odds = prop.get("under_odds")
        adjusted = _simulate_market(
            adjusted_values, float(prop.get("line")), over_odds, under_odds,
            _norm_player_name(prop.get("player")) + market + "negative-review",
            minutes=[], market=market, matchup_adjustment=None,
        )
        if adjusted and over_odds is not None and under_odds is not None:
            adjusted = _apply_historical_calibration(adjusted, market)
            over_raw = _american_probability(over_odds)
            under_raw = _american_probability(under_odds)
            mass = max(1e-9, over_raw + under_raw)
            quote = {
                "over_probability": over_raw / mass,
                "under_probability": under_raw / mass,
                "over_odds": over_odds,
                "under_odds": under_odds,
                "books_used": prop.get("market_books") or ["DraftKings/FanDuel via PropLine"],
                "over_book": "PropLine",
                "under_book": "PropLine",
            }
            adjusted = _score_blended_market(adjusted, quote)
            # Preserve the original line/odds and identity; replace only model output.
            for key in (
                "projection", "over_probability", "under_probability",
                "push_probability", "best_side", "best_probability", "best_odds",
                "expected_value_pct", "edge_pp", "recommendation", "kelly_fraction_pct",
                "model_over_probability", "model_under_probability", "market_over_probability",
                "market_under_probability", "over_ev_pct", "under_ev_pct", "over_edge_pp",
                "under_edge_pp", "selection_reason", "calibration_status", "calibration_applied",
                "calibration_method", "model_components", "model_method",
                "distribution_std_dev", "count_dispersion_k",
            ):
                if key in adjusted:
                    prop[key] = adjusted[key]
            prop["projected_minutes"] = round(recent_median, 1) if recent_median is not None else prop.get("projected_minutes")
            prop["negative_review"]["adjusted_projection"] = adjusted.get("projection")
            prop["negative_review"]["adjusted_side"] = adjusted.get("best_side")
            prop["negative_review"]["adjusted_ev_pct"] = adjusted.get("expected_value_pct")
            prop["negative_review"]["workload_factor"] = round(factor, 3)
            prop["negative_review"]["return_gap_days"] = return_gap_days
            prop["negative_review"]["adjustment_basis"] = (
                "Return after 7+ day absence; current minutes versus prior-season baseline"
                if recent_return else
                "Current-season return-to-play minutes versus prior-season baseline"
                if recent_return else "Recent minutes versus prior-season baseline"
            )
            prop["sample_label"] = sample_label
            prop["workload_adjusted"] = True
            # On a returning/injured player, an OVER is not retained from the
            # unadjusted model. Only a positive-EV UNDER survives the risk review.
            if adjusted.get("best_side") != "UNDER" or float(adjusted.get("expected_value_pct") or 0) <= 0:
                prop["recommendation"] = "PASS"
                prop["best_side"] = None
                prop["best_odds"] = None
                prop["best_probability"] = None
                prop["expected_value_pct"] = None
                prop["edge_pp"] = None
                prop["negative_review"]["status"] = "removed"
                prop["negative_review"]["reason"] = (
                    "Workload/injury adjustment did not produce a positive-EV UNDER."
                )
                return False, prop["negative_review"]["reason"]
            prop["negative_review"]["status"] = "under_value"
            prop["negative_review"]["reason"] = (
                "Workload/injury risk repriced; retained only because the adjusted UNDER remains positive EV."
            )
        else:
            prop["negative_review"] = {
                "status": "inconclusive",
                "reason": "Workload risk found but two-sided odds or adjusted model unavailable",
                "workload_factor": round(factor, 3),
            }
            return False, prop["negative_review"]["reason"]
    else:
        prop["negative_review"] = {
            "status": "clear",
            "reason": "No material official injury or recent workload reduction detected",
            "current_season_games": current_games,
            "recent_minutes_median": round(recent_median, 1) if recent_median is not None else None,
            "prior_minutes_baseline": round(prior_baseline, 1) if prior_baseline is not None else None,
        }

    return True, "Passed post-selection negative review"


def _snapshot_daily_market_top5(market, props):
    """Select initial Top 5, then run targeted negative review and promote replacements."""
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        slate_date = _active_nba_slate_date(now_et)
        prediction_ledger.initialize()
        existing_slate = prediction_ledger.get_daily_slate(market, slate_date)
        if existing_slate and existing_slate.get("candidates"):
            return existing_slate["candidates"]
        if now_et.hour < 7:
            return None
        calibration_rows = []
        for prop in props or []:
            if not isinstance(prop, dict) or prop.get("best_side") not in ("OVER", "UNDER"):
                continue
            if prop.get("best_odds") is None or prop.get("best_probability") is None:
                continue
            sample = int(prop.get("sample_size") or prop.get("games_used") or 0)
            if sample < 4:
                continue
            item = dict(prop)
            item["market"] = market
            item["calibration_sample"] = True
            item["slate_date"] = slate_date
            calibration_rows.append(item)
        if calibration_rows:
            prediction_ledger.save_predictions(calibration_rows, market)
        ordered = sorted(
            [dict(p, market=market) for p in (props or []) if isinstance(p, dict)],
            key=lambda item: float(item.get("expected_value_pct") if item.get("expected_value_pct") is not None else -1e9),
            reverse=True,
        )
        if not ordered:
            return []
        frozen = prediction_ledger.freeze_daily_slate(market, slate_date, ordered)
        candidates = [dict(p) for p in (frozen.get("candidates") or []) if isinstance(p, dict)]
        injury_feed = _load_official_injury_feed()
        eligible = []
        for prop in candidates:
            if prop.get("injury_blocked") or prop.get("injury_flag"):
                continue
            if prop.get("recommendation") not in ("VALUE CANDIDATE", "LEAN / VERIFY"):
                continue
            # Mixed current/prior-season samples are allowed into the risk review;
            # the review, not a blanket label filter, decides whether they survive.
            try:
                ev = float(prop.get("expected_value_pct"))
                sample = int(prop.get("sample_size") or prop.get("games_used") or 0)
                minutes = float(prop.get("projected_minutes"))
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(ev) or ev <= 0 or sample < 8 or minutes < 12:
                continue
            if prop.get("best_side") not in ("OVER", "UNDER") or prop.get("best_odds") is None:
                continue
            eligible.append(prop)
        eligible.sort(key=lambda item: float(item.get("expected_value_pct") or 0), reverse=True)

        # The expensive injury/workload lookups happen only for provisional picks
        # and, if one is removed, the next best candidate needed to fill the slot.
        selected, seen = [], set()
        reviewed = 0
        removed = []
        for prop in eligible:
            key = (_norm_player_name(prop.get("player")), str(prop.get("game") or "").casefold())
            if key in seen:
                continue
            seen.add(key)
            reviewed += 1
            try:
                passed, reason = _post_selection_negative_review(prop, market, injury_feed)
            except Exception as review_error:
                passed = False
                reason = "Negative review failed safely: " + type(review_error).__name__
                prop["negative_review"] = {"status": "review_failed", "reason": reason}
            if passed:
                selected.append(prop)
                if len(selected) == 5:
                    break
            else:
                removed.append({
                    "player": prop.get("player"),
                    "side": prop.get("best_side"),
                    "reason": reason,
                })
        for prop in candidates:
            if prop.get("negative_review") is None:
                prop["negative_review"] = {"status": "not_reviewed", "reason": "Outside provisional Top 5 and not needed as a replacement"}
        if selected:
            for index, prop in enumerate(selected, 1):
                prop["selection_rank"] = index
                prop["post_selection_reviewed"] = True
                side = str(prop.get("best_side") or "").upper()
                line = prop.get("line")
                prop["play_instruction"] = (
                    f"PLAY {side} {line} {market.upper()}"
                    if side in ("OVER", "UNDER") and line is not None
                    else "NO PLAY"
                )
            prediction_ledger.save_daily_top_five(selected, market, slate_date)
            # Keep selected picks first in the frozen board; leave the remaining
            # market candidates available as a replacement audit trail.
            selected_keys = {
                (_norm_player_name(p.get("player")), str(p.get("game") or "").casefold())
                for p in selected
            }
            candidates.sort(
                key=lambda p: (
                    (_norm_player_name(p.get("player")), str(p.get("game") or "").casefold()) not in selected_keys,
                    -float(p.get("expected_value_pct") or -1e9),
                )
            )
        return candidates
    except Exception:
        return None

def _revalidate_frozen_market_candidates(market, candidates, injury_feed):
    """Re-run the post-selection risk gate against today's immutable source snapshot."""
    ordered = sorted(
        [dict(p) for p in candidates if isinstance(p, dict)],
        key=lambda p: float(p.get("expected_value_pct") if p.get("expected_value_pct") is not None else -1e9),
        reverse=True,
    )
    eligible = []
    for p in ordered:
        if p.get("recommendation") not in ("VALUE CANDIDATE", "LEAN / VERIFY"):
            continue
        # Mixed current/prior-season samples must undergo the workload review.
        try:
            ev=float(p.get("expected_value_pct"))
            sample=int(p.get("sample_size") or p.get("games_used") or 0)
            minutes=float(p.get("projected_minutes"))
        except (TypeError,ValueError,OverflowError):
            continue
        if not math.isfinite(ev) or ev <= 0 or sample < 8 or minutes < 12:
            continue
        if p.get("best_side") not in ("OVER","UNDER") or p.get("best_odds") is None:
            continue
        eligible.append(p)
    selected=[]; seen=set()
    for p in eligible:
        key=(_norm_player_name(p.get("player")),str(p.get("game") or "").casefold())
        if key in seen: continue
        seen.add(key)
        try:
            passed, reason=_post_selection_negative_review(p,market,injury_feed)
        except Exception as exc:
            passed=False; reason="Negative review failed safely: "+type(exc).__name__
            p["negative_review"]={"status":"review_failed","reason":reason}
        p["post_selection_reviewed"]=True
        if passed and p.get("best_side") in ("OVER","UNDER") and float(p.get("expected_value_pct") or 0)>0:
            selected.append(p)
            if len(selected)==5: break
    selected_keys={(_norm_player_name(p.get("player")),str(p.get("game") or "").casefold()) for p in selected}
    for p in ordered:
        key=(_norm_player_name(p.get("player")),str(p.get("game") or "").casefold())
        if key not in selected_keys and p.get("negative_review") is None:
            p["negative_review"]={"status":"not_reviewed","reason":"Not selected for targeted post-selection review"}
    for rank,p in enumerate(selected,1): p["selection_rank"]=rank
    if selected:
        prediction_ledger.save_daily_top_five(selected,market,_active_nba_slate_date())
    ordered.sort(key=lambda p: (0 if (_norm_player_name(p.get("player")),str(p.get("game") or "").casefold()) in selected_keys else 1,-float(p.get("expected_value_pct") or -1e9)))
    return ordered

@app.get("/api/proprietary-market-board")
def proprietary_market_board(market: str = "rebounds"):
    market = market.lower().strip()
    if market not in STAT_FIELDS:
        raise HTTPException(status_code=400, detail="Use points, rebounds, assists, steals, or threes.")
    now = time.time()
    cached_board = _BP_BOARD_CACHE.get(market)
    injury_feed = _load_official_injury_feed()
    bdl_games = _load_balldontlie_games(allow_fetch=(market == "points"))
    try:
        today_et = _active_nba_slate_date()
        prediction_ledger.initialize()
        frozen_slate = prediction_ledger.get_daily_slate(market, today_et)
    except Exception:
        frozen_slate = None
    if frozen_slate and frozen_slate.get("candidates"):
        frozen_props = [dict(p) for p in (frozen_slate.get("candidates") or []) if isinstance(p, dict)]
        # Re-run today's targeted injury/workload gate even for an already-frozen
        # snapshot. The original source lines remain intact; unsafe picks are
        # removed from the actionable Top 5 and replaced from the same snapshot.
        frozen_props = _revalidate_frozen_market_candidates(market, frozen_props, injury_feed)
        for prop in frozen_props:
            _apply_official_injury(prop, injury_feed)
            _apply_balldontlie_game(prop, bdl_games)
        return JSONResponse({
            "market": market,
            "source": "Frozen Eastern-time daily slate · original PropLine/DraftKings snapshot",
            "line_count": len(frozen_props),
            "projected_count": len(frozen_props),
            "props": frozen_props,
            "daily_frozen": True,
            "daily_slate_date": today_et,
            "daily_slate_saved_at": frozen_slate.get("created_at"),
            "daily_slate_rule": "Previous slate remains visible until 7:00 a.m. Eastern; then today’s picks are frozen.",
            "unavailable": [],
            "cache_ttl_seconds": _BP_BOARD_TTL_SECONDS,
            "prop_line_cache_source": "daily-snapshot",
            "injury_feed": injury_feed.get("meta", {}),
            "balldontlie": {key: value for key, value in bdl_games.items() if key != "games"},
        })
    if cached_board and now - cached_board[0] < _BP_BOARD_TTL_SECONDS:
        cached_result = _json_safe(cached_board[1])
        for cached_prop in cached_result.get("props") or []:
            _apply_balldontlie_game(cached_prop, bdl_games)
        cached_result["injury_feed"] = injury_feed.get("meta", {})
        cached_result["balldontlie"] = {key: value for key, value in bdl_games.items() if key != "games"}
        frozen_candidates = _snapshot_daily_market_top5(market, cached_result.get("props") or [])
        if frozen_candidates is not None:
            cached_result["props"] = frozen_candidates
        cached_result["daily_frozen"] = frozen_candidates is not None
        cached_result["daily_slate_date"] = _active_nba_slate_date()
        cached_result["daily_slate_rule"] = "Previous slate remains visible until 7:00 a.m. Eastern; then today’s picks are frozen."
        return JSONResponse(cached_result)

    events, props_source, quota = _propline_fetch_events()
    props = _propline_market_props(events, market)
    field, label = STAT_FIELDS[market]
    output, failures = [], []
    for raw in props:
        name = raw.get("player")
        if not name:
            continue
        try:
            line = float(raw.get("line"))
            quote = _propline_quote(raw)
            event_matchup = raw.get("game") or ""
            team_codes = _team_codes_from_game(event_matchup)
            roster_athlete_id, roster_team = _espn_roster_player_id(name, team_codes)
            athlete_id = roster_athlete_id or _espn_player_id(name)
            if not athlete_id:
                failures.append(name + ": ESPN player match unavailable")
                continue
            resolved_team = _canonical_team_code(raw.get("team")) or roster_team or ""
            values, sample_label, player_minutes = _player_history(athlete_id, field, label)
            team_for_dvp = resolved_team
            event_matchup = raw.get("game") or ""
            matchup_adjustment = _dvp_matchup_adjustment(athlete_id, team_for_dvp, event_matchup, market, name)
            model = _simulate_market(
                values, line, None, None, _norm_player_name(name) + market,
                minutes=player_minutes, market=market,
                matchup_adjustment=matchup_adjustment,
            )
            if not model:
                failures.append(name + ": fewer than five valid game logs")
                continue
            model = _apply_historical_calibration(model, market)
            if quote:
                model = _score_blended_market(model, quote)
            else:
                model["recommendation"] = "PASS"
                model["expected_value_pct"] = None
                model["best_side"] = None
                model["best_probability"] = None
                model["best_odds"] = None
                model["edge_pp"] = None
                model["blend_method"] = "No verified two-sided DraftKings quote; no bet rating"

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
            elif model.get("projected_minutes") is not None and float(model["projected_minutes"]) < 15.0:
                model["confidence"] = "low"
                model["recommendation"] = "PASS"
                model["expected_value_pct"] = None
                model["best_side"] = None
                model["best_probability"] = None
                model["best_odds"] = None
                model["edge_pp"] = None
                model["probability_note"] = (
                    "Projection only: projected playing time is below 15 minutes. "
                    "EV ranking is disabled because a low-minute role is too volatile."
                )
            else:
                model.setdefault("confidence", "provisional")

            market_prop = {
                "player": name,
                "team": resolved_team,
                "game": event_matchup,
                "market": market,
                "line": line,
                "over_odds": raw.get("over_odds"),
                "under_odds": raw.get("under_odds"),
                "sample_label": sample_label,
                "athlete_id": athlete_id,
                "games_used": len(values),
                "event_id": raw.get("event_id"),
                "line_source": "DraftKings/FanDuel via PropLine",
                "line_updated_at": raw.get("last_update"),
                "bookmaker_link": raw.get("bookmaker_link"),
                "market_books": raw.get("market_books") or ["Sportsbook"],
                "position": (matchup_adjustment or {}).get("position"),
                "defense_vs_position": matchup_adjustment,
                **model,
            }
            # Injury and return-to-play review is deliberately deferred until
            # after this market's provisional Top 5 has been ranked.
            _apply_balldontlie_game(market_prop, bdl_games)
            output.append(market_prop)
        except Exception as exc:
            failures.append(name + ": " + type(exc).__name__ + ": " + str(exc)[:180])

    output.sort(key=lambda row: (
        row.get("expected_value_pct") is None,
        -(row.get("expected_value_pct") or -999),
    ))
    try:
        prediction_ledger.initialize()
    except Exception:
        pass
    result = {
        "market": market,
        "source": "ESPN game-log distribution + DraftKings/FanDuel odds via PropLine",
        "blend": "Independent proprietary probability; DraftKings/FanDuel prices used only for EV and market-relative edge",
        "line_count": len(props),
        "projected_count": len(output),
        "props": output,
        "unavailable": failures[:20],
        "cache_ttl_seconds": _BP_BOARD_TTL_SECONDS,
        "prop_line_cache_source": props_source,
        "prop_line_quota": quota,
        "injury_feed": injury_feed.get("meta", {}),
        "balldontlie": {key: value for key, value in bdl_games.items() if key != "games"},
    }
    frozen_candidates = _snapshot_daily_market_top5(market, output)
    if frozen_candidates is not None:
        result["props"] = frozen_candidates
        result["line_count"] = len(frozen_candidates)
        result["projected_count"] = len(frozen_candidates)
    result["daily_frozen"] = frozen_candidates is not None
    result["daily_slate_date"] = _active_nba_slate_date()
    result["daily_slate_rule"] = "Previous slate remains visible until 7:00 a.m. Eastern; then today’s picks are frozen."
    _BP_BOARD_CACHE[market] = (time.time(), result)
    return JSONResponse(result)



@app.get("/api/cron/freeze-daily")
def cron_freeze_daily(request: FastRequest):
    """Capture all five market boards at 7 a.m. Eastern."""
    secret = os.getenv("CRON_SECRET", "")
    if not secret or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour != 7:
        return JSONResponse({"status": "outside_freeze_window", "eastern_time": now_et.isoformat(), "required_window": "07:00-07:59 America/New_York"})
    results = {}
    for market_name in ("points", "rebounds", "assists", "steals", "threes"):
        try:
            response = proprietary_market_board(market_name)
            payload = json.loads(response.body.decode("utf-8"))
            results[market_name] = {"frozen": bool(payload.get("daily_frozen")), "candidate_count": len(payload.get("props") or []), "source": payload.get("prop_line_cache_source")}
        except Exception as exc:
            results[market_name] = {"frozen": False, "error": type(exc).__name__ + ": " + str(exc)[:160]}
    return JSONResponse({"status": "completed" if all(v.get("frozen") for v in results.values()) else "partial", "slate_date": now_et.date().isoformat(), "timezone": "America/New_York", "markets": results})

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


_ESPN_LIVE_SUMMARY_CACHE = {}

def _fetch_live_summary(event_id):
    """Fetch and briefly cache one ESPN live game summary for box-score enrichment."""
    key = str(event_id or "").strip()
    if not key:
        return {}
    now = time.time()
    cached = _ESPN_LIVE_SUMMARY_CACHE.get(key)
    if cached and now - cached.get("at", 0.0) < 20:
        return cached.get("data") or {}
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?" + urlencode({"event": key})
    error = None
    try:
        summary = _get_json(url, timeout=8)
        if not isinstance(summary, dict):
            error = "unexpected_response_type"
            summary = {}
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)[:180]
        summary = {}
    _ESPN_LIVE_SUMMARY_CACHE[key] = {"at": now, "data": summary, "error": error}
    # Keep the process-local cache bounded on busy slates.
    if len(_ESPN_LIVE_SUMMARY_CACHE) > 40:
        oldest = sorted(_ESPN_LIVE_SUMMARY_CACHE, key=lambda item: _ESPN_LIVE_SUMMARY_CACHE[item].get("at", 0.0))[:10]
        for item in oldest:
            _ESPN_LIVE_SUMMARY_CACHE.pop(item, None)
    return summary


def _safe_live_score(value):
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0


_LOCAL_LIVE_SCOREBOARD_CACHE = {"at": 0.0, "payload": None}
_LOCAL_LIVE_SCOREBOARD_TTL = 8


@app.get("/api/live-stats")
def live_stats():
    """Serve live NBA scores locally from ESPN, then enrich every active game with its box score.

    This deliberately avoids the retired/stale LIVE_UPSTREAM deployment. The browser
    receives the same stable shape it already consumes: games, players_by_id,
    players_by_name and updated_at.
    """
    now = time.time()
    cached = _LOCAL_LIVE_SCOREBOARD_CACHE.get("payload")
    if cached is None or now - _LOCAL_LIVE_SCOREBOARD_CACHE.get("at", 0.0) >= _LOCAL_LIVE_SCOREBOARD_TTL:
        et_now = datetime.now(ZoneInfo("America/New_York"))
        dates = [et_now.date().isoformat()]  # Keep the live board scoped to today's ET slate; yesterday's players can collide by athlete ID.
        games = []
        errors = []
        seen_events = set()
        for day in dates:
            try:
                scoreboard = _get_json(
                    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?"
                    + urlencode({"dates": day.replace("-", "")}),
                    timeout=10,
                )
            except Exception as exc:
                errors.append(day + ": " + type(exc).__name__)
                continue
            for event in scoreboard.get("events") or []:
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("id") or "").strip()
                if not event_id or event_id in seen_events:
                    continue
                seen_events.add(event_id)
                competitions = event.get("competitions") or []
                competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
                competitors = competition.get("competitors") or []
                away = next((x for x in competitors if x.get("homeAway") == "away"), None)
                home = next((x for x in competitors if x.get("homeAway") == "home"), None)
                if not away or not home:
                    continue
                away_team = away.get("team") or {}
                home_team = home.get("team") or {}
                team_aliases = {
                    "NY": "NYK", "GS": "GSW", "SA": "SAS",
                    "NO": "NOP", "UTAH": "UTA", "WSH": "WAS",
                }
                away_code_raw = str(away_team.get("abbreviation") or "").upper()
                home_code_raw = str(home_team.get("abbreviation") or "").upper()
                away_code = team_aliases.get(away_code_raw, away_code_raw)
                home_code = team_aliases.get(home_code_raw, home_code_raw)
                if not away_code or not home_code:
                    continue
                status = competition.get("status") or event.get("status") or {}
                status_type = status.get("type") or {}
                state = str(status_type.get("state") or "pre").lower()
                games.append({
                    "id": event_id,
                    "event_id": event_id,
                    "game": away_code + "@" + home_code,
                    "away": away_code,
                    "home": home_code,
                    "teams": [away_code, home_code],
                    "away_score": _safe_live_score(away.get("score")),
                    "home_score": _safe_live_score(home.get("score")),
                    "state": state,
                    "period": int(status.get("period") or 0),
                    "clock": str(status.get("displayClock") or ""),
                    "status": str(status_type.get("detail") or status_type.get("description") or "Scheduled"),
                    "date": event.get("date") or competition.get("date"),
                })
        cached = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "games": games,
            "players_by_id": {},
            "players_by_name": {},
            "source": "ESPN scoreboard + ESPN game summaries",
            "scoreboard_errors": errors[:3],
        }
        _LOCAL_LIVE_SCOREBOARD_CACHE.update({"at": now, "payload": cached})

    # Work on a fresh copy because enrichment mutates the player maps.
    payload = json.loads(json.dumps(cached))
    enrichment_error = None
    try:
        payload = _enrich_live_boxscores(payload)
    except Exception as exc:
        # Keep the scoreboard alive even if an upstream box-score schema changes.
        enrichment_error = type(exc).__name__ + ": " + str(exc)[:160]
        payload.setdefault("players_by_id", {})
        payload.setdefault("players_by_name", {})
    payload["live_feed_status"] = {
        "enrichment_error": enrichment_error,
        "source": payload.get("source"),
        "games": len(payload.get("games") or []),
        "players": len(payload.get("players_by_id") or {}),
        "boxscore_enrichment": payload.get("live_enrichment"),
        "scoreboard_errors": payload.get("scoreboard_errors") or [],
    }
    return JSONResponse(payload)


@app.get("/api/live-stats-diagnostic")
def live_stats_diagnostic():
    """Inspect ESPN summary availability and schema without returning player data."""
    try:
        feed_request = Request(LIVE_UPSTREAM + "/api/live-stats", headers={
            "User-Agent": "Mozilla/5.0 ColtsNBAEdge/1.0",
            "Accept": "application/json",
            **({"x-vercel-protection-bypass": os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET")} if os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET") else {}),
        })
        with urlopen(feed_request, timeout=12) as response:
            upstream = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        return JSONResponse({"ok": False, "stage": "live_feed", "error": type(exc).__name__ + ": " + str(exc)[:180]}, status_code=502)
    games = upstream.get("games") if isinstance(upstream, dict) else []
    checked = []
    for game in (games or []):
        if not isinstance(game, dict) or str(game.get("state") or "").lower() not in ("in", "post", "final"):
            continue
        event_id = game.get("event_id") or game.get("id")
        summary = _fetch_live_summary(event_id)
        cached = _ESPN_LIVE_SUMMARY_CACHE.get(str(event_id), {})
        boxscore = summary.get("boxscore") if isinstance(summary, dict) else None
        team_blocks = boxscore.get("players") if isinstance(boxscore, dict) else None
        team_sample = next((x for x in (team_blocks or []) if isinstance(x, dict)), None) if isinstance(team_blocks, list) else None
        statistics = team_sample.get("statistics") if isinstance(team_sample, dict) else None
        group_sample = next((x for x in statistics if isinstance(x, dict)), None) if isinstance(statistics, list) else None
        checked.append({
            "event_id": str(event_id or ""),
            "game_state": game.get("state"),
            "summary_keys": sorted(summary.keys())[:35] if isinstance(summary, dict) else [],
            "fetch_error": cached.get("error"),
            "boxscore_type": type(boxscore).__name__ if boxscore is not None else None,
            "boxscore_keys": sorted(boxscore.keys())[:25] if isinstance(boxscore, dict) else [],
            "players_type": type(team_blocks).__name__ if team_blocks is not None else None,
            "team_block_keys": sorted(team_sample.keys())[:20] if team_sample else [],
            "statistics_type": type(statistics).__name__ if statistics is not None else None,
            "stat_group_keys": sorted(group_sample.keys())[:20] if group_sample else [],
            "stat_labels": (group_sample.get("names") or group_sample.get("labels") or [])[:30] if group_sample else [],
            "athlete_row_keys": sorted((group_sample.get("athletes") or [{}])[0].keys()) if group_sample and group_sample.get("athletes") else [],
            "athlete_sample": ((group_sample.get("athletes") or [{}])[0]) if group_sample and group_sample.get("athletes") else None,
        })
        if len(checked) >= 3:
            break
    return JSONResponse({"ok": True, "game_count": len(games or []), "checked": checked})

def _enrich_live_boxscores(payload):
    """Merge verified ESPN box-score stats and minutes into the live player maps."""
    if not isinstance(payload, dict):
        return payload
    by_id = payload.setdefault("players_by_id", {})
    by_name = payload.setdefault("players_by_name", {})
    if not isinstance(by_id, dict) or not isinstance(by_name, dict):
        payload["live_enrichment"] = {"ok": False, "error": "player_maps_invalid"}
        return payload

    aliases = {
        "MIN": "minutes_played", "MINUTES": "minutes_played",
        "PTS": "points", "POINTS": "points",
        "REB": "rebounds", "REBOUNDS": "rebounds",
        "AST": "assists", "ASSISTS": "assists",
        "STL": "steals", "STEALS": "steals",
        "BLK": "blocks", "BLOCKS": "blocks",
        "3PT": "three_points_made", "3PM": "three_points_made",
        "3PTM": "three_points_made", "3FGM": "three_points_made",
    }
    field_counts = {field: 0 for field in set(aliases.values())}
    games_checked = 0
    athlete_rows = 0
    athletes_enriched = set()
    errors = []

    for game in payload.get("games") or []:
        if not isinstance(game, dict) or str(game.get("state") or "").lower() not in ("in", "post", "final"):
            continue
        event_id = game.get("event_id") or game.get("id")
        if not event_id:
            continue
        games_checked += 1
        summary = _fetch_live_summary(event_id)
        boxscore = summary.get("boxscore") if isinstance(summary, dict) else None
        team_blocks = boxscore.get("players") if isinstance(boxscore, dict) else None
        if not isinstance(team_blocks, list):
            cached = _ESPN_LIVE_SUMMARY_CACHE.get(str(event_id), {})
            if cached.get("error"):
                errors.append(str(event_id) + ": " + cached["error"])
            continue

        for team_block in team_blocks:
            if not isinstance(team_block, dict):
                continue
            for stat_group in team_block.get("statistics") or []:
                if not isinstance(stat_group, dict):
                    continue
                labels = stat_group.get("names")
                if not isinstance(labels, list) or not labels:
                    labels = stat_group.get("labels")
                # ESPN's v2 summary uses "keys" for the stat-column labels
                # (e.g. MIN, FG, 3PT, FT, REB, AST, STL, BLK, PTS).
                if not isinstance(labels, list) or not labels:
                    labels = stat_group.get("keys") or []
                if not isinstance(labels, list):
                    continue
                indexes = {}
                for index, label in enumerate(labels):
                    key = str(label or "").strip().upper().replace(" ", "")
                    if key in aliases:
                        indexes[index] = aliases[key]
                athlete_list = stat_group.get("athletes") or []
                if not isinstance(athlete_list, list):
                    continue

                for athlete_row in athlete_list:
                    if not isinstance(athlete_row, dict):
                        continue
                    athlete_rows += 1
                    athlete = athlete_row.get("athlete") or {}
                    if not isinstance(athlete, dict):
                        athlete = {}
                    athlete_id = str(athlete.get("id") or athlete_row.get("id") or "").strip()
                    name = str(athlete.get("displayName") or athlete.get("fullName") or athlete.get("name") or "").strip()
                    values = athlete_row.get("stats") or []
                    if not isinstance(values, list) or not (athlete_id or name):
                        continue
                    updates = {}
                    for index, field in indexes.items():
                        if index >= len(values):
                            continue
                        raw = str(values[index] or "").strip()
                        if not raw or raw in ("-", "--"):
                            continue
                        try:
                            if field == "minutes_played" and ":" in raw:
                                mm, ss = raw.split(":", 1)
                                value = float(mm) + float(ss) / 60.0
                            elif field == "three_points_made" and "-" in raw:
                                value = float(raw.split("-", 1)[0])
                            else:
                                value = float(raw)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(value) and value >= 0:
                            updates[field] = round(value, 2)
                    if not updates:
                        continue

                    if athlete_id:
                        target = by_id.setdefault(athlete_id, {})
                        if not isinstance(target, dict):
                            target = by_id[athlete_id] = {}
                        target.update(updates)
                        target.setdefault("player", name or target.get("player") or "")
                        target["athlete_id"] = athlete_id
                        target["game_id"] = str(event_id)
                    if name:
                        name_key = _quarter_name_key(name)
                        target = by_name.setdefault(name_key, {})
                        if not isinstance(target, dict):
                            target = by_name[name_key] = {}
                        target.update(updates)
                        target.setdefault("player", name)
                        if athlete_id:
                            target["athlete_id"] = athlete_id
                        target["game_id"] = str(event_id)
                    if athlete_id or name:
                        athletes_enriched.add(athlete_id or _quarter_name_key(name))
                    for field in updates:
                        field_counts[field] += 1

    payload["players_by_id"] = by_id
    payload["players_by_name"] = by_name
    payload["live_enrichment"] = {
        "ok": True,
        "games_checked": games_checked,
        "athlete_rows_seen": athlete_rows,
        "athletes_enriched": len(athletes_enriched),
        "field_updates": field_counts,
        "errors": errors[:3],
    }
    return payload

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
        summary = _fetch_live_summary(event_id)
        if not summary:
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



_TANK01_DVP_PROBE_CACHE = {"at": 0.0, "data": None}
_TANK01_CACHE_TTL = 21600
_TANK01_HOST = "tank01-fantasy-stats.p.rapidapi.com"


def _tank01_request(path, params=None, timeout=15):
    """Call Tank01 through RapidAPI without ever exposing credentials."""
    api_key = os.getenv("TANK01_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TANK01_API_KEY is not configured.")
    query = urlencode(params or {})
    url = "https://" + _TANK01_HOST + path + (("?" + query) if query else "")
    request = Request(url, headers={
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": _TANK01_HOST,
        "Accept": "application/json",
        "User-Agent": "ColtsNBAEdge/1.0",
    })
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
        headers = response.headers
    if not isinstance(payload, dict):
        raise ValueError("Tank01 returned an unexpected response type.")
    status_code = payload.get("statusCode")
    if status_code not in (None, 200, "200"):
        raise RuntimeError("Tank01 endpoint returned status " + str(status_code) + ".")
    if payload.get("error"):
        raise RuntimeError("Tank01 endpoint error: " + str(payload.get("error"))[:160])
    return payload, headers


@app.get("/api/tank01-dvp-probe")
def tank01_dvp_probe():
    """Safe first-call diagnostic: validates Tank01 and reports schema keys only."""
    now = time.time()
    cached = _TANK01_DVP_PROBE_CACHE.get("data")
    if cached and now - _TANK01_DVP_PROBE_CACHE.get("at", 0.0) < 900:
        return JSONResponse(cached)
    if not os.getenv("TANK01_API_KEY", "").strip():
        return JSONResponse({
            "configured": False, "authenticated": False,
            "message": "TANK01_API_KEY is not configured in this deployment."
        }, status_code=503)
    try:
        payload, headers = _tank01_request(
            "/getNBATeams", {"teamStats": "true"}, timeout=20
        )
        body = payload.get("body")
        if isinstance(body, dict):
            teams = list(body.values()) if body and all(isinstance(v, dict) for v in body.values()) else []
        elif isinstance(body, list):
            teams = body
        else:
            teams = []
        sample = next((team for team in teams if isinstance(team, dict) and team.get("defensiveStats")), None)
        defensive = sample.get("defensiveStats") if sample else None
        schema = {
            "team_keys": sorted(sample.keys()) if sample else [],
            "defensive_stats_type": type(defensive).__name__ if defensive is not None else None,
            "defensive_stats_keys": sorted(defensive.keys()) if isinstance(defensive, dict) else [],
            "defensive_stats_sample_keys": {
                str(key): sorted(value.keys()) if isinstance(value, dict) else type(value).__name__
                for key, value in list((defensive or {}).items())[:8]
            } if isinstance(defensive, dict) else {},
            "defensive_stats_sample_values": {
                str(key): {str(pos): value for pos, value in list(item.items())[:6]}
                for key, item in list((defensive or {}).items())[:8]
                if isinstance(item, dict)
            } if isinstance(defensive, dict) else {},
            "games_played": defensive.get("gamesPlayed") if isinstance(defensive, dict) else None,
        }
        output = {
            "configured": True,
            "authenticated": bool(teams),
            "source": "Tank01 RapidAPI",
            "endpoint_tested": "/getNBATeams?teamStats=true",
            "team_count": len(teams),
            "teams_with_defensive_stats": sum(
                1 for team in teams if isinstance(team, dict) and team.get("defensiveStats")
            ),
            "schema": schema,
            "rate_limit_remaining": headers.get("x-ratelimit-requests-remaining"),
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "message": "Tank01 authentication succeeded; schema inspection completed."
                if sample else "Tank01 responded, but no team defensiveStats object was found.",
        }
        _TANK01_DVP_PROBE_CACHE.update({"at": now, "data": output})
        return JSONResponse(output)
    except HTTPError as exc:
        message = "RapidAPI rejected the Tank01 request." if exc.code in (401, 403) else "Tank01 returned HTTP " + str(exc.code) + "."
        return JSONResponse({
            "configured": True, "authenticated": False, "http_status": exc.code,
            "endpoint_tested": "/getNBATeams?teamStats=true", "message": message,
        }, status_code=502)
    except Exception as exc:
        return JSONResponse({
            "configured": True, "authenticated": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:220],
        }, status_code=502)



@app.get("/api/tank01-dvp-status")
def tank01_dvp_status():
    """Report availability and sample size for Tank01 current-season positional tables."""
    try:
        tables, meta = _fetch_tank01_dvp_tables()
        ready = all(len(table) >= 150 for table in tables.values())
        active_tables = {}
        active_sources = {}
        for market in ("PTS", "REB", "AST", "STL", "3PM"):
            try:
                table = _fetch_dvp_table(market)
                active_tables[market] = len(table)
                active_sources[market] = _DVP_STAT_CACHE.get(market, {}).get("season")
            except Exception as source_error:
                active_tables[market] = 0
                active_sources[market] = "unavailable: " + type(source_error).__name__
        all_active = all(count >= 120 for count in active_tables.values())
        return JSONResponse({
            "configured": True,
            "authenticated": True,
            "tank01_source": meta.get("source"),
            "tank01_current_season_ready": ready,
            "tank01_min_games_played_per_team": meta.get("min_games_played"),
            "tank01_max_games_played_per_team": meta.get("max_games_played"),
            "tank01_teams": meta.get("team_count"),
            "tank01_position_rows_by_market": {market: len(table) for market, table in tables.items()},
            "active_dvp_tables_ready": all_active,
            "active_dvp_rows_by_market": active_tables,
            "active_dvp_source_by_market": active_sources,
            "markets": list(tables.keys()),
            "fallback_policy": "Use current-season Tank01 DVP immediately when all 30 teams and five positions are present; sample sizes are reported for transparency.",
            "rate_limit_remaining": meta.get("rate_limit_remaining"),
            "message": "Tank01 current-season DVP is active, with early-season sample sizes disclosed." if ready and all_active
                else "Tank01 feed is authenticated, but one or more current-season positional tables are incomplete."
        })
    except Exception as exc:
        return JSONResponse({
            "configured": bool(os.getenv("TANK01_API_KEY", "").strip()),
            "authenticated": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:220],
        }, status_code=502)


@app.get("/api/model/calibration")
def model_calibration(market: str = "all"):
    """Expose per-market calibration readiness and a same-sample no-vig benchmark."""
    selected = market.lower().strip()
    if selected != "all" and selected not in STAT_FIELDS:
        raise HTTPException(status_code=400, detail="Use points, rebounds, assists, steals, threes, or all.")
    try:
        prediction_ledger.initialize()
        names = list(STAT_FIELDS) if selected == "all" else [selected]
        profiles = {name: prediction_ledger.calibration_profile(name) for name in names}
        ready = [name for name, profile in profiles.items() if profile.get("ready")]
        total = sum(int(profile.get("sample_size") or 0) for profile in profiles.values())
        return JSONResponse({
            "status": "calibrated" if ready and len(ready) == len(names) else (
                "collecting settled pregame outcomes" if total == 0 else
                "insufficient settled sample; raw probabilities remain unadjusted"
            ),
            "minimum_sample_size_per_market": 100,
            "minimum_bin_sample_size": 20,
            "minimum_populated_bins": 3,
            "calibrated_markets": ready,
            "markets": profiles,
            "policy": "Sportsbook probabilities are benchmarked, never blended into the proprietary model. Calibration is applied only when each market independently meets all evidence thresholds.",
        })
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Calibration history unavailable: " + type(exc).__name__)


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
    scoreboard_cache = {}
    summary_cache = {}

    def completed_boxscore_value(athlete_id, field, label, slate_date, expected_teams):
        """Use completed ESPN scoreboard events when athlete gamelogs lack event IDs."""
        try:
            base_date = datetime.fromisoformat(str(slate_date)[:10]).date()
        except Exception:
            return None
        matches = []
        for offset in (-1, 0, 1):
            date_key = (base_date + timedelta(days=offset)).strftime("%Y%m%d")
            if date_key not in scoreboard_cache:
                try:
                    scoreboard_cache[date_key] = _get_json(
                        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?"
                        + urlencode({"dates": date_key}), timeout=8
                    )
                except Exception:
                    scoreboard_cache[date_key] = {}
            for event in (scoreboard_cache[date_key] or {}).get("events") or []:
                event_id = str(event.get("id") or "")
                status = ((event.get("status") or {}).get("type") or {})
                if not event_id or not status.get("completed"):
                    continue
                teams = set()
                competitions = event.get("competitions") or []
                competitors = competitions[0].get("competitors") or [] if competitions else []
                for competitor in competitors:
                    team = competitor.get("team") or {}
                    code = _canonical_team_code(team.get("abbreviation") or team.get("shortDisplayName"))
                    if code:
                        teams.add(team_aliases.get(code, code))
                if len(teams) != 2:
                    teams = matchup_for_event(event_id) or set()
                if expected_teams and not (
                    teams == expected_teams or
                    (len(expected_teams) == 1 and expected_teams.issubset(teams))
                ):
                    continue
                if event_id not in summary_cache:
                    try:
                        summary_cache[event_id] = _get_json(
                            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?"
                            + urlencode({"event": event_id}), timeout=8
                        )
                    except Exception:
                        summary_cache[event_id] = {}
                groups = ((summary_cache[event_id] or {}).get("boxscore") or {}).get("players") or []
                found_values = []
                for group in groups:
                    group_team = group.get("team") or {}
                    group_code = _canonical_team_code(group_team.get("abbreviation") or group_team.get("shortDisplayName"))
                    if expected_teams and len(expected_teams) == 1 and group_code and group_code not in expected_teams:
                        continue
                    for stat_group in group.get("statistics") or []:
                        labels = stat_group.get("labels") or []
                        aliases = {"3PT": ("3PM", "3P"), "PTS": ("POINTS",),
                                   "REB": ("REBOUNDS",), "AST": ("ASSISTS",), "STL": ("STEALS",)}
                        idx = labels.index(label) if label in labels else next(
                            (labels.index(a) for a in aliases.get(label, ()) if a in labels), -1
                        )
                        if idx < 0:
                            continue
                        for athlete in stat_group.get("athletes") or []:
                            athlete_info = athlete.get("athlete") or {}
                            if str(athlete_info.get("id") or "") != str(athlete_id):
                                continue
                            stats = athlete.get("stats") or []
                            if idx >= len(stats):
                                continue
                            raw = str(stats[idx]).strip()
                            if label == "3PT" and "-" in raw:
                                raw = raw.split("-", 1)[0].strip()
                            try:
                                value = float(raw)
                            except (TypeError, ValueError):
                                continue
                            if math.isfinite(value) and value >= 0:
                                found_values.append(value)
                if found_values:
                    matches.extend((event_id, value) for value in found_values)
        unique = {event_id: value for event_id, value in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    result = {"checked": 0, "settled": 0, "skipped": 0, "errors": 0,
              "skip_reasons": {"missing_identity": 0, "no_matching_completed_game": 0,
                               "ambiguous_game": 0, "settlement_conflict": 0},
              "settlements": []}

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
                # ESPN summary responses vary: some expose competitors under
                # header.competitions, others expose the two teams under boxscore.
                header = payload.get("header") or {}
                competitions = header.get("competitions") or []
                competitors = []
                if isinstance(competitions, list) and competitions:
                    competitors = competitions[0].get("competitors") or []
                if not competitors:
                    boxscore = payload.get("boxscore") or {}
                    team_rows = boxscore.get("teams") or []
                    competitors = [
                        row.get("team") if isinstance(row, dict) and isinstance(row.get("team"), dict) else row
                        for row in team_rows
                    ]
                teams = set()
                for competitor in competitors or []:
                    if not isinstance(competitor, dict):
                        continue
                    team = competitor.get("team") if isinstance(competitor.get("team"), dict) else competitor
                    code = _canonical_team_code(team.get("abbreviation") or team.get("shortDisplayName"))
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
            game = str(snap.get("game") or "")
            # DB timestamps are stored in UTC. A pick saved during the evening
            # in North American time can therefore have a UTC creation date one day
            # after its local slate/game date. Prefer the immutable saved slate date.
            created_date = str(
                snap.get("slate_date")
                or candidate.get("created_date")
                or ""
            )
            # Some board feeds omit the matchup string. In that case, a verified
            # player ID plus the saved team and an ESPN event containing that team
            # is sufficient; never settle by player name alone.
            matchup = re.match(
                r"^([A-Z]{2,3})@([A-Z]{2,3})",
                game.upper().replace(" ", "")
            )
            expected_teams = _team_codes_from_game(game)
            if len(expected_teams) != 2 and matchup:
                expected_teams = {
                    team_aliases.get(code, code) for code in matchup.groups()
                }
            candidate_team = _canonical_team_code(
                snap.get("team") or snap.get("roster_team")
            )
            if not expected_teams and candidate_team:
                expected_teams = {candidate_team}
            if (
                not athlete_id.isdigit()
                or not mapping
                or not created_date
            ):
                result["skipped"] += 1
                result["skip_reasons"]["missing_identity"] += 1
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
                    # ESPN dates can be UTC while slate dates are local.
                    # Permit the adjacent prior calendar date for late-evening
                    # Eastern picks, while still requiring the exact verified
                    # player ID and matchup below.
                    if not (created - timedelta(days=1) <= game_date <= created + timedelta(days=2)):
                        continue
                    event_teams = matchup_for_event(event_id)
                    if not event_teams:
                        continue
                    # If the saved pick included both teams, require an exact
                    # matchup. If it only included the player's team, require
                    # that team to be one of the two verified event participants.
                    if (
                        expected_teams
                        and (
                            event_teams == expected_teams
                            or (
                                len(expected_teams) == 1
                                and expected_teams.issubset(event_teams)
                            )
                        )
                    ):
                        possible.append(row)
                # Fallback to exact athlete-ID stats in completed ESPN box scores.
                if len(possible) == 0:
                    boxscore_actual = completed_boxscore_value(
                        athlete_id, field, label, created_date, expected_teams
                    )
                    if boxscore_actual is not None:
                        possible = [{"value": boxscore_actual}]
                # More than one possible event is ambiguous; do not settle it.
                if len(possible) != 1:
                    result["skipped"] += 1
                    result["skip_reasons"][
                        "ambiguous_game" if len(possible) > 1 else "no_matching_completed_game"
                    ] += 1
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
                result["skip_reasons"]["settlement_conflict"] += 1
            except Exception as exc:
                result["errors"] += 1
                # Keep the response actionable without leaking URLs, tokens, or credentials.
                result.setdefault("error_types", {})
                error_name = type(exc).__name__
                result["error_types"][error_name] = result["error_types"].get(error_name, 0) + 1
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


# A dashboard history refresh also acts as a live settlement heartbeat.
# The browser polls this endpoint every 60 seconds; throttle per warm serverless
# instance so multiple open tabs do not repeatedly query ESPN.
_AUTO_SETTLEMENT_LAST_RUN = 0.0
_AUTO_SETTLEMENT_MIN_INTERVAL = 45.0


@app.get("/api/model/daily-top5")
def get_daily_top5(slate_date: str, market: str = "all"):
    global _AUTO_SETTLEMENT_LAST_RUN
    try:
        prediction_ledger.initialize()
        now = time.time()
        settlement_status = {"checked": 0, "settled": 0, "skipped": 0, "errors": 0}
        if now - _AUTO_SETTLEMENT_LAST_RUN >= _AUTO_SETTLEMENT_MIN_INTERVAL:
            _AUTO_SETTLEMENT_LAST_RUN = now
            settlement_response = _run_pending_settlements()
            try:
                settlement_status = json.loads(settlement_response.body.decode("utf-8"))
            except Exception:
                settlement_status = {"status": "unavailable"}
        return JSONResponse({
            "predictions": prediction_ledger.list_daily_top_five(slate_date, market),
            "all_time": prediction_ledger.daily_top_five_metrics(),
            "settlement": settlement_status,
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
        # Persist the user's sportsbook line before applying the availability
        # gate, so an injured player's saved line is never lost.
        saved = prediction_ledger.save_predictions([model], market)
        _apply_official_injury(model, _load_official_injury_feed(), player, payload.get("team"))
        return JSONResponse({"prop": model, "ledger": saved})
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Enter a numeric line and valid American over/under odds.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Player history or prediction database is temporarily unavailable.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not recalculate and save this custom line.") from exc


@app.get("/api/advanced-metrics/status")
def advanced_metrics_status():
    try:
        return JSONResponse(advanced_metrics.status())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Advanced-metrics status unavailable: " + type(exc).__name__)


@app.api_route("/api/advanced-metrics/collect", methods=["GET", "POST"])
def collect_advanced_metrics(request: FastRequest, batch: int = None):
    """Vercel Cron collector. Requires the project's CRON_SECRET bearer token."""
    secret = os.getenv("CRON_SECRET", "").strip()
    supplied = request.headers.get("authorization", "")
    cron_authorized = bool(secret) and secrets.compare_digest(supplied, "Bearer " + secret)
    preview_authorized = (
        os.getenv("VERCEL_ENV", "") == "preview"
        and os.getenv("VERCEL_GIT_COMMIT_REF", "") == "sides-totals-rebuild"
        and os.getenv("METRICS_PREVIEW_RUN", "").strip().lower() == "true"
    )
    if not cron_authorized and not preview_authorized:
        raise HTTPException(status_code=401, detail="Unauthorized metrics collection request.")
    try:
        return JSONResponse(advanced_metrics.collect(batch))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Advanced-metrics collection failed: " + type(exc).__name__)


@app.get("/api/game-board")
def game_sides_totals_board(refresh: bool = False, preview_date: str = ""):
    """Return the independent point-spread and game-total board."""
    try:
        return JSONResponse(game_model.build_board(force=refresh,preview_date=preview_date or None))
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc).replace(os.getenv("PROPLINE_API_KEY", "___never_match___"), "[redacted]")
        return JSONResponse({
            "status": "unavailable",
            "error": type(exc).__name__,
            "detail": message[:300],
            "board_type": "sides_and_totals",
        }, status_code=503)


@app.get("/api/game-history")
def game_sides_totals_history(slate_date: str):
    try:
        return JSONResponse({
            "slate_date": slate_date,
            "predictions": game_model.history(slate_date),
            "metrics": game_model._metrics(),
        })
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Game-model history is temporarily unavailable: " + type(exc).__name__)


@app.get("/api/game-metrics")
def game_sides_totals_metrics():
    try:
        return JSONResponse(game_model._metrics())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Game-model record is temporarily unavailable: " + type(exc).__name__)




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

                    injury_feed = _load_official_injury_feed()
                    bdl_games = _load_balldontlie_games()
                    for prop in props:
                        if not isinstance(prop, dict):
                            continue
                        _apply_official_injury(
                            prop, injury_feed, prop.get("player"),
                            prop.get("roster_team") or prop.get("team")
                        )

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
                            if prop.get("injury_blocked") or prop.get("injury_flag"):
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
                    for prop in props:
                        _apply_balldontlie_game(prop, bdl_games)
                    board_payload["injury_feed"] = injury_feed.get("meta", {})
                    board_payload["balldontlie"] = {key: value for key, value in bdl_games.items() if key != "games"}
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
                    live_payload = _enrich_live_boxscores(live_payload)
                    live_payload = _enrich_live_quarters(live_payload)
                    response_body = json.dumps(live_payload, separators=(",", ":")).encode("utf-8")
                except Exception:
                    pass
            return Response(content=response_body, status_code=response.status, media_type=media_type)
    except HTTPError as e:
        return Response(content=e.read(), status_code=e.code, media_type=e.headers.get("content-type", "application/json"))
    except (URLError, TimeoutError) as e:
        raise HTTPException(status_code=502, detail="Upstream API unavailable") from e