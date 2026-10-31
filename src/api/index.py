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
ESPN_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
ESPN_SEARCH = "https://site.web.api.espn.com/apis/search/v2"
STAT_FIELDS = {
    "rebounds": ("rebounds", "REB"),
    "assists": ("assists", "AST"),
    "steals": ("steals", "STL"),
    "threes": ("threePointsMade", "3PT"),
}
_ESPN_ID_CACHE = {}
_ESPN_LOG_CACHE = {}
_ESPN_ROSTER_CACHE = {}

_ESPN_POSITION_CACHE = {}
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

 # Use current-season positional data only once the source reports at least
 # 10 games for every team. Until then, use the completed prior season.
 try:
  current_table,current_text=fetch_and_parse("season")
 except Exception:
  current_table,current_text=None,""
 sample=re.search(r"\((\d+)\s*[–-]\s*(\d+)\s+games?\s+a\s+team\)",current_text,re.I)
 if current_table is not None and sample and int(sample.group(1))>=10:
  parsed,season=current_table,"current season"
 else:
  parsed,_=fetch_and_parse("prev")
  season="previous season (current season sample below 10 games or unavailable)"
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
def _dvp_matchup_adjustment(athlete_id,player_team,game,market):
 try:
  market=str(market or "").lower().strip()
  stat_fields={"points":"PTS","rebounds":"REB","assists":"AST","steals":"STL","threes":"3PM"}
  stat=stat_fields.get(market)
  if not stat: return None
  team_code=_canonical_team_code(player_team); game_teams=_team_codes_from_game(game)
  if not team_code or len(game_teams)<2: return None
  opponent=next((code for code in game_teams if code!=team_code),None)
  if not opponent: return None
  position_key=_espn_player_position(athlete_id,team_code)
  if not position_key: return None
  position_keys={"G":("PG","SG"),"F":("SF","PF")}.get(position_key,(position_key,))
  table=_fetch_dvp_table(stat)
  allowed_values=[table.get((opponent,pos),{}).get(stat) for pos in position_keys]
  if any(value is None for value in allowed_values): return None
  allowed=sum(allowed_values)/len(allowed_values)
  league_values=[row[stat] for (team,pos),row in table.items() if pos in position_keys and stat in row]
  if len(league_values)<20: return None
  league_average=sum(league_values)/len(league_values)
  if league_average<=0: return None
  ratio=allowed/league_average
  factor=max(0.92,min(1.08,1.0+(ratio-1.0)*0.30))
  position_names={"PG":"Point Guard","SG":"Shooting Guard","SF":"Small Forward","PF":"Power Forward","C":"Center",
   "G":"Guard (PG/SG blended)","F":"Forward (SF/PF blended)"}
  stat_labels={"points":"points","rebounds":"rebounds","assists":"assists","steals":"steals","threes":"3-pointers made"}
  stat_units={"points":"PPG","rebounds":"RPG","assists":"APG","steals":"SPG","threes":"3PM"}
  season=_DVP_STAT_CACHE.get(stat,{}).get("season") or "season not identified"
  position_method="PG/SG blended" if position_key=="G" else "SF/PF blended" if position_key=="F" else "Individual position"
  return {"position":position_key,"position_group":position_key,"position_name":position_names[position_key],
   "opponent":opponent,"opponent_name":_TEAM_NAMES.get(opponent,opponent),
   "stat":market,"stat_label":stat_labels[market],"stat_unit":stat_units[market],
   "allowed_per_game":round(allowed,2),"league_position_average":round(league_average,2),
   "league_relative_ratio":round(ratio,3),"adjustment_pct":round((factor-1.0)*100,2),"factor":round(factor,4),
   "source":"NBA House individual-position "+stat+" allowed table","source_season":season,
   "method":position_method+" "+stat_labels[market]+" allowed per game vs matching-position league average; 30% dampening; adjustment capped at +/-8%"}
 except Exception: return None

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
            if _norm_player_name(athlete_name) == normalized_name:
                matches[(athlete_id, team_code.upper())] = athlete_name

    if len(matches) == 1:
        athlete_id, team_code = next(iter(matches))
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

def _espn_player_id(name):
    """Resolve a player to ESPN's numeric NBA athlete ID.

    ESPN global search can return UUID-style content IDs. The common-v3
    gamelog endpoint requires the numeric athlete ID used in ESPN profile
    URLs / athlete UIDs, so never pass an arbitrary search-result UUID.
    """
    key = _norm_player_name(name)
    if key in _ESPN_ID_CACHE:
        return _ESPN_ID_CACHE[key]

    query = urlencode({"query": name, "limit": 20, "sport": "basketball", "league": "nba"})
    payload = _get_json(ESPN_SEARCH + "?" + query)
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
    if len(current_stats) < 5:
        prior_stats = _gamelog_records(athlete_id, field, label, 2026)
        prior_minutes = _gamelog_records(athlete_id, "minutes", "MIN", 2026)

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
    if len(current_stats) < 5 and prior_stats:
        # Prior-season scoring records remain valid even if ESPN's separate
        # stat/minute categories cannot be joined. Minutes are optional; never
        # throw away verified scoring history solely because that join failed.
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
    """Recency-weighted empirical projection with minutes adjustment and discrete outcomes."""
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
    # Recent form is a capped signal, not a replacement for the season baseline.
    form_weight = min(0.30, len(recent10)/(len(recent10)+20.0))
    target_mean = (1.0-form_weight)*long_mean + form_weight*weighted_mean

    valid_minutes = []
    for item in (minutes or []):
        try:
            value=float(item)
            if math.isfinite(value) and value>0: valid_minutes.append(value)
        except (TypeError,ValueError):
            continue
    projected_min = _projected_minutes(valid_minutes)
    baseline_min = _median(valid_minutes[-20:]) if valid_minutes else None
    minute_factor = 1.0
    if projected_min is not None and baseline_min and baseline_min > 0 and len(valid_minutes) >= 8:
        # Minutes are a projection adjustment, not a second full projection.
        # Tight bounds reduce false swings caused by short-term rotations/noise.
        minute_factor = max(0.85, min(1.15, projected_min / baseline_min))
        target_mean *= minute_factor

    matchup_factor = 1.0
    if isinstance(matchup_adjustment, dict):
        try:
            candidate = float(matchup_adjustment.get("factor", 1.0))
            if math.isfinite(candidate):
                matchup_factor = max(0.92, min(1.08, candidate))
                target_mean *= matchup_factor
        except (TypeError, ValueError):
            matchup_factor = 1.0

    distribution=[max(0.0,v*(target_mean/long_mean)) if long_mean>0 else v for v in clean]
    ordered=sorted(distribution)
    median=ordered[len(ordered)//2]
    deviations=sorted(abs(x-median) for x in ordered)
    mad=deviations[len(deviations)//2] if deviations else 0.0
    variance=sum((x-target_mean)**2 for x in distribution)/max(1,len(distribution)-1)
    std_dev=max(math.sqrt(max(0.0,variance)),1.4826*mad,0.25)
    # A pure empirical bootstrap assigns zero probability to outcomes beyond
    # the observed sample. Blend it with a wider parametric component so small
    # samples do not create false near-certainty (for example, 99.9% unders).
    parametric_weight=0.25 if market in ("steals","threes") else 0.35
    uncertainty_factor=min(1.25,1.0+0.50/math.sqrt(n))
    rng=random.Random(str(seed)+"|"+str(line)+"|"+str(market))
    sims=60000
    over=under=push=0
    for _ in range(sims):
        if rng.random()<parametric_weight:
            sample=max(0.0,rng.gauss(target_mean,std_dev*uncertainty_factor))
        else:
            sample=distribution[rng.randrange(len(distribution))]
        if abs(sample-line)<1e-9: push+=1
        elif sample>line: over+=1
        else: under+=1
    over_p,under_p,push_p=over/sims,under/sims,push/sims
    over_ev=_odds_profit(over_p,under_p,over_odds)
    under_ev=_odds_profit(under_p,over_p,under_odds)
    best=max([(v,side,prob,price) for v,side,prob,price in [
        (over_ev,"OVER",over_p,over_odds),(under_ev,"UNDER",under_p,under_odds)
    ] if v is not None],key=lambda x:x[0],default=None)
    return {
        "projection":round(target_mean,2),
        "projected_minutes":round(projected_min,1) if projected_min is not None else None,
        "over_probability":round(over_p*100,2),"under_probability":round(under_p*100,2),
        "push_probability":round(push_p*100,2),"simulations":sims,
        "best_side":best[1] if best else None,
        "best_probability":round(best[2]*100,2) if best else None,
        "best_odds":best[3] if best else None,
        "expected_value_pct":round(best[0]*100,2) if best else None,
        "edge_pp":None,
        "recommendation":"VALUE CANDIDATE" if best and best[0]>0.05 else ("LEAN / VERIFY" if best and best[0]>0 else "PASS"),
        "model_components":["exponentially weighted history","10-game form signal","minutes adjustment" if valid_minutes else "minutes unavailable; no adjustment","defense-vs-position matchup adjustment" if matchup_adjustment else "defense-vs-position unavailable; no adjustment","empirical/parametric mixture distribution","Monte Carlo probability estimation"],
        "model_method":"recency-weighted empirical/parametric mixture with minutes adjustment; provisional pending out-of-sample calibration",
        "sample_size":n,"long_run_mean":round(long_mean,2),"recent5_mean":round(recent5_mean,2),        "recent10_mean":round(recent10_mean,2),"weighted_mean":round(weighted_mean,2),
        "distribution_std_dev":round(std_dev,2),"minute_adjustment_factor":round(minute_factor,3),
        "minutes_sample_size":len(valid_minutes),
        "matchup_adjustment_pct":round((matchup_factor - 1.0) * 100, 2),
        "defense_vs_position":matchup_adjustment,
        "baseline_minutes":round(baseline_min,1) if baseline_min is not None else None,
        "minutes_method":"55% recent-5 median / 30% recent-10 median / 15% recent-20 median; adjustment capped at +/-15%",
        "calibration_status":"uncalibrated"
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

def _score_blended_market(model, market_quote):
    """
    Logarithmic opinion pool on the conditional Over/Under probabilities.
    Push probability remains a separate model outcome, avoiding the previous
    distortion from independently pooling two non-complementary probabilities.
    """
    model_over = model["over_probability"] / 100.0
    model_under = model["under_probability"] / 100.0
    model_push = max(0.0, min(0.25, model.get("push_probability", 0.0) / 100.0))
    model_decision_mass = max(1e-9, model_over + model_under)
    model_over_cond = model_over / model_decision_mass
    market_over = market_quote["over_probability"]
    market_under = market_quote["under_probability"]
    market_mass = max(1e-9, market_over + market_under)
    market_over_cond = market_over / market_mass
    # Fixed 50% market weight is a transparent starting point, not a learned
    # optimum. The response exposes this so calibration can replace it later.
    market_weight = 0.50
    blended_over_cond = _log_pool(model_over_cond, market_over_cond, market_weight)
    blended_under_cond = 1.0 - blended_over_cond
    over = (1.0 - model_push) * blended_over_cond
    under = (1.0 - model_push) * blended_under_cond
    choices = []
    for side, prob, loss, odds in (
        ("OVER", over, under, market_quote["over_odds"]),
        ("UNDER", under, over, market_quote["under_odds"]),
    ):
        ev = _odds_profit(prob, loss, odds)
        if ev is not None:
            choices.append((ev, side, prob, odds, loss))
    best = max(choices, key=lambda x: x[0], default=None)
    model.update({
        "model_over_probability": round(model_over * 100, 2),
        "model_under_probability": round(model_under * 100, 2),
        "market_over_probability": round(market_over * 100, 2),
        "market_under_probability": round(market_under * 100, 2),
        "over_probability": round(over * 100, 2),
        "under_probability": round(under * 100, 2),
        "push_probability": round(model_push * 100, 2),
        "best_side": best[1] if best else None,
        "best_probability": round(best[2] * 100, 2) if best else None,
        "best_odds": best[3] if best else None,
        "expected_value_pct": round(best[0] * 100, 2) if best else None,
        "edge_pp": round((best[2] - _american_probability(best[3])) * 100, 2) if best else None,
        "recommendation": "VALUE CANDIDATE" if best and best[0] > 0.05 else ("LEAN / VERIFY" if best and best[0] > 0 else "PASS"),
        "kelly_fraction_pct": round(max(0.0, (best[2] * _american_payout(best[3]) - best[4]) /
            max(1e-9, _american_payout(best[3]) * (best[2] + best[4]))) * 100, 2) if best else None,
        "blend_method": "50/50 conditional logarithmic pool; market weight awaits out-of-sample optimization",
        "market_weight": market_weight,
        "market_books": market_quote["books_used"],
        "best_over_book": market_quote["over_book"],
        "best_under_book": market_quote["under_book"],
        "calibration_status": "uncalibrated",
    })
    return model

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
            matchup_adjustment = _dvp_matchup_adjustment(athlete_id, team_for_dvp, event.get("name") or raw.get("event_name") or raw.get("matchup") or "", market)
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
                "game": event.get("name") or raw.get("event_name") or raw.get("matchup") or "",
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
        matchup_adjustment = _dvp_matchup_adjustment(athlete_id, payload.get("team"), payload.get("game"), market)
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
                            matchup_adjustment = _dvp_matchup_adjustment(athlete_id, roster_team, prop.get("game"), "points")
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