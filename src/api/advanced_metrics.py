"""NBA advanced-metrics collection and immutable daily snapshots.

The collector stores source rows as received; it does not manufacture missing
metrics. League-wide team/player dashboards are collected every run, while
team-specific on/off and five-man lineup endpoints rotate across six batches.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_BASE = "https://stats.nba.com/stats/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}
_TEAMS = [
    ("ATL",1610612737),("BOS",1610612738),("BKN",1610612751),("CHA",1610612766),
    ("CHI",1610612741),("CLE",1610612739),("DAL",1610612742),("DEN",1610612743),
    ("DET",1610612765),("GSW",1610612744),("HOU",1610612745),("IND",1610612754),
    ("LAC",1610612746),("LAL",1610612747),("MEM",1610612763),("MIA",1610612748),
    ("MIL",1610612749),("MIN",1610612750),("NOP",1610612740),("NYK",1610612752),
    ("OKC",1610612760),("ORL",1610612753),("PHI",1610612755),("PHX",1610612756),
    ("POR",1610612757),("SAC",1610612758),("SAS",1610612759),("TOR",1610612761),
    ("UTA",1610612762),("WAS",1610612764),
]

def _season():
    now = datetime.now(timezone.utc)
    start = now.year if now.month >= 7 else now.year - 1
    return f"{start}-{str(start + 1)[-2:]}"

def _db():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)

def initialize():
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nba_metric_snapshots (
          id BIGSERIAL PRIMARY KEY,
          snapshot_date DATE NOT NULL,
          captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          season TEXT NOT NULL,
          metric_key TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          source TEXT NOT NULL,
          payload JSONB NOT NULL,
          UNIQUE(snapshot_date, metric_key, entity_key)
        );
        CREATE INDEX IF NOT EXISTS nba_metric_snapshots_lookup
          ON nba_metric_snapshots(metric_key, snapshot_date DESC);
        CREATE TABLE IF NOT EXISTS nba_metric_collection_runs (
          id BIGSERIAL PRIMARY KEY,
          snapshot_date DATE NOT NULL,
          season TEXT NOT NULL,
          batch INTEGER NOT NULL,
          started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          finished_at TIMESTAMPTZ,
          status TEXT NOT NULL,
          summary JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS nba_metric_runs_date
          ON nba_metric_collection_runs(snapshot_date DESC, started_at DESC);
        """)

def _fetch(endpoint, params):
    req = Request(_BASE + endpoint + "?" + urlencode(params), headers=_HEADERS)
    with urlopen(req, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    sets = payload.get("resultSets") or payload.get("resultSet") or []
    if isinstance(sets, dict):
        sets = [sets]
    output = []
    for result_set in sets:
        if not isinstance(result_set, dict):
            continue
        headers = result_set.get("headers") or []
        rows = result_set.get("rowSet") or []
        if not headers:
            continue
        name = str(result_set.get("name") or "result").lower()
        for row in rows:
            if isinstance(row, (list, tuple)):
                output.append((name, dict(zip(headers, row))))
    if not output:
        raise RuntimeError(f"{endpoint} returned no tabular rows.")
    return output


def _bdl_get(path, params=None, timeout=12):
    api_key = os.getenv("BALLDONTLIE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BALLDONTLIE_API_KEY is not configured.")
    query = urlencode(params or [])
    url = "https://api.balldontlie.io" + path + (("?" + query) if query else "")
    req = Request(url, headers={"Authorization":api_key,"Accept":"application/json","User-Agent":"ColtsNBAEdge/2.0"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8","replace")), dict(response.headers.items())
    except Exception as exc:
        from urllib.error import HTTPError
        if isinstance(exc, HTTPError):
            raise RuntimeError("BALLDONTLIE " + path + " returned HTTP " + str(exc.code) + "; endpoint may require GOAT tier.") from exc
        raise

def _tank01_team_rows():
    from . import game_model
    ratings = game_model._tank01_ratings(force=True)
    rows = []
    for code, values in (ratings.get("teams") or {}).items():
        rows.append(("tank01_team", {
            "TEAM_ABBREVIATION":code, "GP":values.get("games"),
            "OFF_RATING":values.get("ortg"), "DEF_RATING":values.get("drtg"),
            "NET_RATING":values.get("net"), "PACE":values.get("pace"),
            "EFG_PCT":values.get("efg"), "TM_TOV_PCT":values.get("tov"),
            "OREB_PCT":values.get("oreb"), "TS_PCT":values.get("ts"),
            "source_detail":values.get("source"), "prior_weight":values.get("prior_weight"),
            "recent_sample":values.get("recent_sample")
        }))
    if len(rows) < 28:
        raise RuntimeError("Tank01 returned only " + str(len(rows)) + "/30 usable team records.")
    return rows

def _bdl_team_season_advanced(season):
    payload, headers = _bdl_get("/nba/v1/team_season_averages/general", [
        ("season",season),("season_type","regular"),("type","advanced"),("per_page",100)
    ])
    rows = payload.get("data")
    if not isinstance(rows,list) or not rows:
        raise RuntimeError("BALLDONTLIE team season advanced returned no data.")
    return [("team_season_advanced",row) for row in rows]

def _bdl_player_season_advanced(season):
    payload, headers = _bdl_get("/nba/v1/season_averages/general", [
        ("season",season),("season_type","regular"),("type","advanced")
    ])
    rows = payload.get("data")
    if not isinstance(rows,list) or not rows:
        raise RuntimeError("BALLDONTLIE player season advanced returned no data.")
    return [("player_season_advanced",row) for row in rows]

def _bdl_daily_advanced(snapshot_date):
    day = snapshot_date.isoformat()
    rows = []
    cursor = None
    # Three 100-row pages cover most slates while respecting trial rate limits.
    for page in range(3):
        params = [("dates[]",day),("period",0),("per_page",100)]
        if cursor is not None:
            params.append(("cursor",str(cursor)))
        payload, headers = _bdl_get("/nba/v2/stats/advanced", params)
        page_rows = payload.get("data")
        if not isinstance(page_rows,list):
            raise RuntimeError("BALLDONTLIE daily advanced stats response was invalid.")
        rows.extend(("game_advanced",row) for row in page_rows)
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor or len(page_rows) < 100:
            break
    return rows


def _bdl_projected_lineups(snapshot_date):
    payload, headers = _bdl_get("/nba/v1/projected_lineups", [
        ("dates[]",snapshot_date.isoformat()),("per_page",100)
    ])
    rows = payload.get("data")
    if not isinstance(rows,list):
        raise RuntimeError("BALLDONTLIE projected lineups response was invalid.")
    return [("projected_lineup",row) for row in rows]

def _base_params(season):
    return {
        "LeagueID":"00", "Season":season, "SeasonType":"Regular Season",
        "PerMode":"PerGame", "PlusMinus":"N", "PaceAdjust":"N", "Rank":"N",
        "LastNGames":"0", "Month":"0", "OpponentTeamID":"0", "Location":"",
        "Outcome":"", "DateFrom":"", "DateTo":"", "SeasonSegment":"",
        "VsConference":"", "VsDivision":"", "GameSegment":"", "Period":"0",
    }

def _entity(metric, row, team_code=None):
    if metric in ("team_advanced","team_season_advanced"):
        team = row.get("TEAM") or row.get("team") or {}
        return str(row.get("TEAM_ID") or row.get("TEAM_ABBREVIATION") or team.get("id") or team.get("abbreviation") or "")
    if metric in ("player_advanced","player_season_advanced"):
        player = row.get("PLAYER") or row.get("player") or {}
        return str(row.get("PLAYER_ID") or row.get("player_id") or player.get("id") or "")
    if metric == "team_onoff":
        parts = [team_code, row.get("PLAYER_ID"), row.get("VS_PLAYER_ID"),
                 row.get("GROUP_ID"), row.get("GROUP_NAME"), row.get("GROUP_VALUE"),
                 row.get("GROUP_SET")]
        return ":".join(str(x) for x in parts if x not in (None, ""))
    if metric == "lineups_5":
        parts = [team_code, row.get("GROUP_ID"), row.get("GROUP_NAME"),
                 row.get("GROUP_VALUE"), row.get("GROUP_SET")]
        return ":".join(str(x) for x in parts if x not in (None, ""))
    if metric == "schedule_context":
        return str(row.get("id") or row.get("event_id") or "")
    if metric == "game_advanced":
        game = row.get("game") or {}
        player = row.get("player") or {}
        return str(game.get("id","")) + ":" + str(player.get("id","")) + ":" + str(row.get("period",0))
    if metric == "projected_lineups":
        return str(row.get("game_id","")) + ":" + str(row.get("team_id",""))
    if metric == "injury_status":
        return ":".join(str(x) for x in (row.get("player"),row.get("team"),row.get("game_date")) if x)
    return str(row.get("TEAM_ID") or row.get("PLAYER_ID") or "league")

def _persist(snapshot_date, season, metric, rows, team_code=None, source="NBA Stats"):
    saved = 0
    with _db() as conn:
        for index, (set_name, row) in enumerate(rows):
            entity = _entity(metric, row, team_code)
            if not entity or entity == str(team_code or ""):
                entity = f"{team_code or 'league'}:row:{index}"
            payload = {"result_set":set_name, **row}
            conn.execute("""
              INSERT INTO nba_metric_snapshots
                (snapshot_date,season,metric_key,entity_key,source,payload)
              VALUES (%s,%s,%s,%s,%s,%s::jsonb)
              ON CONFLICT(snapshot_date,metric_key,entity_key)
              DO UPDATE SET captured_at=NOW(), payload=EXCLUDED.payload
            """, (snapshot_date,season,metric,entity,source,json.dumps(payload,default=str)))
            saved += 1
    return saved

def _team_request(metric, endpoint, team_id, season):
    params = _base_params(season)
    params.update({"TeamID":str(team_id), "MeasureType":"Advanced"})
    if metric == "lineups_5":
        params.update({"GroupQuantity":"5"})
    return _fetch(endpoint, params)

def collect(batch=None):
    """Collect reliable team, schedule and injury data plus licensed advanced feeds."""
    initialize()
    season = _season()
    snapshot_date = datetime.now(timezone.utc).date()
    if batch is None:
        batch = datetime.now(timezone.utc).timetuple().tm_yday % 6
    batch = int(batch)
    if batch < 0 or batch > 5:
        raise ValueError("batch must be between 0 and 5")
    selected = _TEAMS[batch * 5:(batch + 1) * 5]
    with _db() as conn:
        previous = conn.execute("""
          SELECT id,status,summary FROM nba_metric_collection_runs
          WHERE snapshot_date=%s AND batch=%s AND status='complete'
          ORDER BY started_at DESC LIMIT 1
        """, (snapshot_date,batch)).fetchone()
    if previous:
        return {"success":True,"status":"already_complete","run_id":previous[0],
                "snapshot_date":snapshot_date.isoformat(),"batch":batch,"summary":previous[2]}

    started = time.time()
    summary = {"season":season, "snapshot_date":snapshot_date.isoformat(),
               "batch":batch, "teams_in_batch":[x[0] for x in selected],
               "datasets":{}, "errors":{}}
    with _db() as conn:
        run_id = conn.execute("""
          INSERT INTO nba_metric_collection_runs(snapshot_date,season,batch,status)
          VALUES (%s,%s,%s,'running') RETURNING id
        """, (snapshot_date,season,batch)).fetchone()[0]

    def schedule_rows():
        from . import game_model
        rows = game_model._schedule(force=True)
        return [("schedule", row) for row in rows]

    def injury_rows():
        from . import index as app_index
        feed = app_index._load_official_injury_feed()
        records = feed.get("records") or []
        meta = feed.get("meta") or {}
        if not records:
            return [("feed_status", {"source_status":meta.get("status","unavailable"),
                                     "source":meta.get("source","official injury report"),
                                     "record_count":0,"error":meta.get("error")})]
        return [("injury", row) for row in records]

    tasks = [
        ("team_advanced", _tank01_team_rows, None, "Tank01"),
        ("schedule_context", schedule_rows, None, "App schedule"),
        ("injury_status", injury_rows, None, "Official NBA injury report"),
        ("team_season_advanced", lambda: _bdl_team_season_advanced(season.split("-")[0]), None, "BALLDONTLIE GOAT"),
        ("game_advanced", lambda: _bdl_daily_advanced(snapshot_date), None, "BALLDONTLIE GOAT"),
        ("projected_lineups", lambda: _bdl_projected_lineups(snapshot_date), None, "BALLDONTLIE GOAT"),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn):(key,team,source) for key,fn,team,source in tasks}
        for future in as_completed(futures):
            key, team_code, source = futures[future]
            metric = key.split(":")[0]
            try:
                rows = future.result()
                saved = _persist(snapshot_date,season,metric,rows,team_code,source)
                summary["datasets"][key] = {"rows_received":len(rows),"rows_saved":saved,"source":source}
            except Exception as exc:
                summary["errors"][key] = f"{type(exc).__name__}: {str(exc)[:220]}"

    summary["elapsed_seconds"] = round(time.time()-started,2)
    successful = len(summary["datasets"])
    required = {"team_advanced","schedule_context","injury_status"}
    required_ok = required.issubset(summary["datasets"])
    status = "complete" if required_ok else ("partial" if successful else "failed")
    summary["optional_tier_note"] = "BALLDONTLIE season advanced, game advanced and projected-lineup feeds require GOAT tier."
    with _db() as conn:
        conn.execute("""
          UPDATE nba_metric_collection_runs
          SET finished_at=NOW(),status=%s,summary=%s::jsonb WHERE id=%s
        """, (status,json.dumps(summary),run_id))
    return {"success":successful > 0,"status":status,"run_id":run_id,**summary}

def latest_team_ratings(max_age_days=2):
    """Return the latest complete persisted NBA Stats team-advanced snapshot."""
    initialize()
    with _db() as conn:
        rows = conn.execute("""
          SELECT metric_key,payload FROM nba_metric_snapshots
          WHERE metric_key IN ('team_advanced','team_season_advanced')
            AND snapshot_date >= (CURRENT_DATE - (%s * INTERVAL '1 day'))::date
          ORDER BY CASE WHEN metric_key='team_season_advanced' THEN 0 ELSE 1 END,
                   snapshot_date DESC
        """, (int(max_age_days),)).fetchall()

    def number(row, key, default=0.0):
        try:
            value = row.get(key)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    bdl_teams = {}
    tank_teams = {}
    for metric, payload in rows:
        if metric == "team_season_advanced":
            team = payload.get("team") or {}
            stats = payload.get("stats") or {}
            code = str(team.get("abbreviation") or "").upper()
            games = int(number(stats,"gp",0))
            ortg = number(stats,"offensive_rating",number(stats,"off_rating",0))
            drtg = number(stats,"defensive_rating",number(stats,"def_rating",0))
            if len(code) == 3 and games > 0 and ortg > 0 and drtg > 0 and code not in bdl_teams:
                bdl_teams[code] = {
                    "team":code,"games":games,"ortg":ortg,"drtg":drtg,
                    "net":number(stats,"net_rating",ortg-drtg),
                    "pace":number(stats,"pace"),
                    "efg":number(stats,"effective_field_goal_percentage",number(stats,"efg_pct",0)),
                    "tov":number(stats,"turnover_ratio",number(stats,"turnover_percentage",0)),
                    "oreb":number(stats,"offensive_rebound_percentage",0),
                    "dreb":number(stats,"defensive_rebound_percentage",0),
                    "ts":number(stats,"true_shooting_percentage",0),
                    "source":"BALLDONTLIE team season advanced snapshot",
                    "recent_sample":games,
                }
            continue

        code = str(payload.get("TEAM_ABBREVIATION") or "").upper()
        if len(code) != 3 or code in tank_teams:
            continue
        games = int(number(payload,"GP",payload.get("games",0)))
        ortg = number(payload,"OFF_RATING",payload.get("ortg",0))
        drtg = number(payload,"DEF_RATING",payload.get("drtg",0))
        if games < 1 or ortg <= 0 or drtg <= 0:
            continue
        tank_teams[code] = {
            "team":code,"games":games,"ortg":ortg,"drtg":drtg,
            "net":number(payload,"NET_RATING",payload.get("net",ortg-drtg)),
            "pace":number(payload,"PACE",payload.get("pace",0)),
            "efg":number(payload,"EFG_PCT",payload.get("efg",0)),
            "tov":number(payload,"TM_TOV_PCT",payload.get("tov",0)),
            "oreb":number(payload,"OREB_PCT",payload.get("oreb",0)),
            "dreb":number(payload,"DREB_PCT",payload.get("dreb",0)),
            "ts":number(payload,"TS_PCT",payload.get("ts",0)),
            "source":"Tank01 persisted team efficiency snapshot",
            "recent_sample":0,
        }

    if len(bdl_teams) >= 28:
        teams = bdl_teams
        quality = "advanced"
        source = "BALLDONTLIE team season advanced; persisted daily snapshot"
    elif len(tank_teams) >= 28:
        teams = tank_teams
        quality = "derived_early_season"
        source = "Tank01 current-season box-score averages; estimated possessions, league-mean shrinkage, persisted daily snapshot"
    else:
        return None
    if len(teams) < 28:
        return None
    min_games = min(row["games"] for row in teams.values())
    return {
        "teams":teams, "season":_season(), "prior_season":None,
        "source":source,
        "data_quality":quality, "fetched_at":datetime.now(timezone.utc).isoformat(),
        "player_impact_source":"Team/player on-off and five-man lineup snapshots are being collected separately; EPM is optional.",
        "epm_available":False,
    }

def status():
    initialize()
    with _db() as conn:
        latest = conn.execute("""
          SELECT metric_key, MAX(snapshot_date)::text AS snapshot_date,
                 COUNT(*) FILTER (WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM nba_metric_snapshots s2 WHERE s2.metric_key=s.metric_key)) AS rows
          FROM nba_metric_snapshots s GROUP BY metric_key ORDER BY metric_key
        """).fetchall()
        runs = conn.execute("""
          SELECT id,snapshot_date::text,season,batch,status,started_at,finished_at,summary
          FROM nba_metric_collection_runs ORDER BY started_at DESC LIMIT 12
        """).fetchall()
    return {
        "datasets":[{"metric":r[0],"latest_snapshot":r[1],"rows":r[2]} for r in latest],
        "recent_runs":[{"id":r[0],"date":r[1],"season":r[2],"batch":r[3],"status":r[4],
                        "started_at":r[5].isoformat() if r[5] else None,
                        "finished_at":r[6].isoformat() if r[6] else None,
                        "summary":r[7]} for r in runs],
        "rotation":"Five teams per batch; six batches rotate daily, completing a full team on/off and lineup cycle every six days.",
        "optional_player_impact":"BALLDONTLIE season/game advanced stats and projected lineups require GOAT tier; Dunks & Threes EPM requires DUNKS_AND_THREES_API_KEY.",
    }
