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

def _base_params(season):
    return {
        "LeagueID":"00", "Season":season, "SeasonType":"Regular Season",
        "PerMode":"PerGame", "PlusMinus":"N", "PaceAdjust":"N", "Rank":"N",
        "LastNGames":"0", "Month":"0", "OpponentTeamID":"0", "Location":"",
        "Outcome":"", "DateFrom":"", "DateTo":"", "SeasonSegment":"",
        "VsConference":"", "VsDivision":"", "GameSegment":"", "Period":"0",
    }

def _entity(metric, row, team_code=None):
    if metric == "team_advanced":
        return str(row.get("TEAM_ID") or row.get("TEAM_ABBREVIATION") or "")
    if metric == "player_advanced":
        return str(row.get("PLAYER_ID") or "")
    if metric == "team_onoff":
        parts = [team_code, row.get("PLAYER_ID"), row.get("VS_PLAYER_ID"),
                 row.get("GROUP_ID"), row.get("GROUP_NAME"), row.get("GROUP_VALUE"),
                 row.get("GROUP_SET")]
        return ":".join(str(x) for x in parts if x not in (None, ""))
    if metric == "lineups_5":
        parts = [team_code, row.get("GROUP_ID"), row.get("GROUP_NAME"),
                 row.get("GROUP_VALUE"), row.get("GROUP_SET")]
        return ":".join(str(x) for x in parts if x not in (None, ""))
    return str(row.get("TEAM_ID") or row.get("PLAYER_ID") or "league")

def _persist(snapshot_date, season, metric, rows, team_code=None):
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
              VALUES (%s,%s,%s,%s,'NBA Stats',%s::jsonb)
              ON CONFLICT(snapshot_date,metric_key,entity_key)
              DO UPDATE SET captured_at=NOW(), payload=EXCLUDED.payload
            """, (snapshot_date,season,metric,entity,json.dumps(payload,default=str)))
            saved += 1
    return saved

def _team_request(metric, endpoint, team_id, season):
    params = _base_params(season)
    params.update({"TeamID":str(team_id), "MeasureType":"Advanced"})
    if metric == "lineups_5":
        params.update({"GroupQuantity":"5"})
    return _fetch(endpoint, params)

def collect(batch=None):
    """Collect league-wide advanced tables and one rotating five-team detail batch."""
    initialize()
    season = _season()
    snapshot_date = datetime.now(timezone.utc).date()
    if batch is None:
        batch = datetime.now(timezone.utc).timetuple().tm_yday % 6
    batch = int(batch)
    if batch < 0 or batch > 5:
        raise ValueError("batch must be between 0 and 5")
    selected = _TEAMS[batch * 5:(batch + 1) * 5]
    started = time.time()
    summary = {"season":season, "snapshot_date":snapshot_date.isoformat(),
               "batch":batch, "teams_in_batch":[x[0] for x in selected],
               "datasets":{}, "errors":{}}
    with _db() as conn:
        run_id = conn.execute("""
          INSERT INTO nba_metric_collection_runs(snapshot_date,season,batch,status)
          VALUES (%s,%s,%s,'running') RETURNING id
        """, (snapshot_date,season,batch)).fetchone()[0]

    tasks = [
        ("team_advanced", lambda: _fetch("leaguedashteamstats",
            {**_base_params(season),"MeasureType":"Advanced"}), None),
        ("player_advanced", lambda: _fetch("leaguedashplayerstats",
            {**_base_params(season),"MeasureType":"Advanced"}), None),
    ]
    for code, team_id in selected:
        tasks.append((f"team_onoff:{code}", lambda tid=team_id: _team_request(
            "team_onoff","teamplayeronoffsummary",tid,season), code))
        tasks.append((f"lineups_5:{code}", lambda tid=team_id: _team_request(
            "lineups_5","teamdashlineups",tid,season), code))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn):(key,team) for key,fn,team in tasks}
        for future in as_completed(futures):
            key, team_code = futures[future]
            metric = key.split(":")[0]
            try:
                rows = future.result()
                saved = _persist(snapshot_date,season,metric,rows,team_code)
                summary["datasets"][key] = {"rows_received":len(rows),"rows_saved":saved}
            except Exception as exc:
                summary["errors"][key] = f"{type(exc).__name__}: {str(exc)[:220]}"

    summary["elapsed_seconds"] = round(time.time()-started,2)
    successful = len(summary["datasets"])
    status = "complete" if successful == len(tasks) else ("partial" if successful else "failed")
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
          SELECT payload FROM nba_metric_snapshots
          WHERE metric_key='team_advanced'
            AND snapshot_date >= (CURRENT_DATE - (%s * INTERVAL '1 day'))::date
          ORDER BY snapshot_date DESC
        """, (int(max_age_days),)).fetchall()
    teams = {}
    def number(row, key, default=0.0):
        try:
            value = row.get(key)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default
    for (payload,) in rows:
        code = str(payload.get("TEAM_ABBREVIATION") or "").upper()
        if len(code) != 3 or code in teams:
            continue
        games = int(number(payload, "GP", 0))
        ortg = number(payload, "OFF_RATING")
        drtg = number(payload, "DEF_RATING")
        if games < 1 or ortg <= 0 or drtg <= 0:
            continue
        teams[code] = {
            "team":code, "games":games, "ortg":ortg, "drtg":drtg,
            "net":number(payload, "NET_RATING", ortg-drtg),
            "pace":number(payload, "PACE"),
            "efg":number(payload, "EFG_PCT"),
            "tov":number(payload, "TM_TOV_PCT"),
            "oreb":number(payload, "OREB_PCT"),
            "dreb":number(payload, "DREB_PCT"),
            "ts":number(payload, "TS_PCT"),
            "source":"NBA Stats persisted advanced snapshot",
            "recent_sample":0,
        }
    if len(teams) < 28:
        return None
    min_games = min(row["games"] for row in teams.values())
    quality = "advanced" if min_games >= 10 else "derived_early_season"
    return {
        "teams":teams, "season":_season(), "prior_season":None,
        "source":"NBA Stats LeagueDashTeamStats advanced; persisted daily snapshot",
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
        "optional_player_impact":"Dunks & Threes Team EPM requires DUNKS_AND_THREES_API_KEY; it is not assumed available.",
    }
