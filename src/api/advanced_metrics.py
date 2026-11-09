"""NBA advanced-metrics collection and immutable daily snapshots.

The collector stores source rows as received; it does not manufacture missing
metrics. Tank01 supplies a fallback team baseline; BALLDONTLIE GOAT adds
official team/player advanced data and projected lineups when authorized.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

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


def _tank01_player_rows():
    from . import index as app_index
    payload, headers = app_index._tank01_request(
        "/getNBATeams", {"rosters":"true","statsToGet":"averages"}, timeout=25
    )
    body = payload.get("body")
    teams = body if isinstance(body,list) else ([v for v in body.values() if isinstance(v,dict)] if isinstance(body,dict) else [])
    rows = []
    def value(stats, *keys):
        for key in keys:
            try:
                raw = stats.get(key)
                if raw is not None:
                    if isinstance(raw,dict):
                        raw = raw.get("Average") or raw.get("Total") or raw.get("avg")
                    return float(raw)
            except (TypeError,ValueError):
                continue
        return 0.0
    for team in teams:
        if not isinstance(team,dict):
            continue
        team_code = str(team.get("teamAbv") or "").upper()
        roster = team.get("Roster") or team.get("roster") or {}
        if isinstance(roster,dict):
            players = list(roster.items())
        elif isinstance(roster,list):
            players = [(str(p.get("playerID") or p.get("id") or i),p) for i,p in enumerate(roster) if isinstance(p,dict)]
        else:
            players = []
        for player_id, player in players:
            if not isinstance(player,dict):
                continue
            stats = player.get("stats") or player.get("playerStats") or {}
            if not isinstance(stats,dict) or not stats:
                continue
            fga = value(stats,"fga","FG_ATT")
            fgm = value(stats,"fgm","FG_M")
            fg3m = value(stats,"tptfgm","fg3m","threePM")
            fta = value(stats,"fta","FT_ATT")
            pts = value(stats,"pts","points")
            tov = value(stats,"TOV","tov","turnovers")
            ast = value(stats,"ast","assists")
            minutes = value(stats,"mins","min","minutes")
            efg = (fgm + 0.5*fg3m)/fga if fga > 0 else None
            ts = pts/(2*(fga+0.44*fta)) if (fga+0.44*fta) > 0 else None
            tov_rate = tov/(fga+0.44*fta+tov) if (fga+0.44*fta+tov) > 0 else None
            row = {
                "player_id":str(player.get("playerID") or player.get("id") or player_id),
                "player_name":player.get("longName") or player.get("playerName") or player.get("name") or "",
                "team_abbreviation":team_code,
                "season":_season(),
                "stats":stats,
                "derived": {
                    "efg_pct":efg, "true_shooting_pct":ts,
                    "turnover_rate":tov_rate,
                    "assist_to_turnover":ast/tov if tov > 0 else None,
                    "usage_events_per_minute":(fga+0.44*fta+tov)/minutes if minutes > 0 else None,
                },
                "derived_metric_note":"Box-score-derived rates only; not EPM, on/off, or possession-adjusted impact."
            }
            rows.append(("tank01_player",row))
    if not rows:
        raise RuntimeError("Tank01 roster endpoint returned no player season-average rows.")
    return rows

def _entity(metric, row, team_code=None):
    if metric in ("team_advanced","team_season_advanced"):
        team = row.get("TEAM") or row.get("team") or {}
        return str(row.get("TEAM_ID") or row.get("TEAM_ABBREVIATION") or team.get("id") or team.get("abbreviation") or "")
    if metric in ("player_advanced","player_season_advanced","player_boxscore_season"):
        player = row.get("PLAYER") or row.get("player") or {}
        return str(row.get("PLAYER_ID") or row.get("player_id") or player.get("id") or "")
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

def collect(batch=None):
    """Collect reliable team, schedule and injury data plus licensed advanced feeds."""
    initialize()
    season = _season()
    snapshot_date = datetime.now(timezone.utc).date()
    # One daily collector now gathers all required feeds; the legacy batch
    # column remains at zero for compatibility with existing run history.
    if batch not in (None, 0, "0"):
        raise ValueError("The advanced-metrics collector no longer uses team batches.")
    batch = 0
    with _db() as conn:
        previous = conn.execute("""
          SELECT id,status,summary FROM nba_metric_collection_runs
          WHERE snapshot_date=%s AND batch=%s AND status IN ('complete','core_complete')
          ORDER BY started_at DESC LIMIT 1
        """, (snapshot_date,batch)).fetchone()
    if previous:
        return {"success":True,"status":"already_complete","run_id":previous[0],
                "snapshot_date":snapshot_date.isoformat(),"batch":batch,"summary":previous[2]}

    started = time.time()
    summary = {"season":season, "snapshot_date":snapshot_date.isoformat(),
               "advanced_games_date":(snapshot_date-timedelta(days=1)).isoformat(),
               "batch":batch, "datasets":{}, "errors":{}}
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
        if meta.get("status") != "fresh":
            raise RuntimeError("Official injury report is not fresh; refusing to snapshot stale or unavailable statuses.")
        if not records:
            return [("feed_status", {"source_status":"fresh_no_listed_injuries",
                                     "source":meta.get("source","official injury report"),
                                     "record_count":0})]
        return [("injury", row) for row in records]

    tasks = [
        ("team_advanced", _tank01_team_rows, None, "Tank01"),
        ("player_boxscore_season", _tank01_player_rows, None, "Tank01"),
        ("schedule_context", schedule_rows, None, "App schedule"),
        ("injury_status", injury_rows, None, "Official NBA injury report"),
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
    status = ("complete" if required_ok and not summary["errors"]
              else "core_complete" if required_ok
              else "partial" if successful else "failed")
    with _db() as conn:
        conn.execute("""
          UPDATE nba_metric_collection_runs
          SET finished_at=NOW(),status=%s,summary=%s::jsonb WHERE id=%s
        """, (status,json.dumps(summary),run_id))
    return {"success":successful > 0,"status":status,"run_id":run_id,**summary}

def latest_team_ratings(max_age_days=2):
    """Return complete persisted Tank01-derived team ratings; no paid GOAT feed is used."""
    initialize()
    with _db() as conn:
        rows = conn.execute("""
          SELECT metric_key,payload FROM nba_metric_snapshots
          WHERE metric_key = 'team_advanced'
            AND snapshot_date >= (CURRENT_DATE - (%s * INTERVAL '1 day'))::date
          ORDER BY snapshot_date DESC
        """, (int(max_age_days),)).fetchall()

    def number(row, key, default=0.0):
        try:
            value = row.get(key)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    tank_teams = {}
    for metric, payload in rows:
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

    if len(tank_teams) < 28:
        return None
    teams = tank_teams
    quality = "derived_early_season"
    source = "Tank01 current-season box-score averages; estimated possessions, league-mean shrinkage, persisted daily snapshot"
    min_games = min(row["games"] for row in teams.values())
    if len(teams) < 28:
        return None
    min_games = min(row["games"] for row in teams.values())
    return {
        "teams":teams, "season":_season(), "prior_season":None,
        "source":source,
        "data_quality":quality, "fetched_at":datetime.now(timezone.utc).isoformat(),
        "player_impact_source":"Tank01 box-score-derived player rates only. NBA Data Parquet (2026-27) is being schema-validated; no player-impact adjustment is applied until its fields and calculations pass validation.",
        "epm_available":False,
    }

def status():
    initialize()
    with _db() as conn:
        latest = conn.execute("""
          SELECT metric_key, MAX(snapshot_date)::text AS snapshot_date,
                 COUNT(*) FILTER (WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM nba_metric_snapshots s2 WHERE s2.metric_key=s.metric_key)) AS rows,
                 (SELECT s2.source FROM nba_metric_snapshots s2
                  WHERE s2.metric_key=s.metric_key
                  ORDER BY s2.snapshot_date DESC,s2.captured_at DESC LIMIT 1) AS source
          FROM nba_metric_snapshots s GROUP BY metric_key ORDER BY metric_key
        """).fetchall()
        runs = conn.execute("""
          SELECT id,snapshot_date::text,season,batch,status,started_at,finished_at,summary
          FROM nba_metric_collection_runs ORDER BY started_at DESC LIMIT 12
        """).fetchall()
    return {
        "datasets":[{"metric":r[0],"latest_snapshot":r[1],"rows":r[2],"source":r[3]} for r in latest],
        "recent_runs":[{"id":r[0],"date":r[1],"season":r[2],"batch":r[3],"status":r[4],
                        "started_at":r[5].isoformat() if r[5] else None,
                        "finished_at":r[6].isoformat() if r[6] else None,
                        "summary":r[7]} for r in runs],
        "rotation":"Daily Tank01 team/player box-score snapshots plus schedule and fresh official injury snapshots. NBA Data Parquet is being validated for possession and lineup impact; it is not yet allowed to change projections.",
        "optional_player_impact":"No paid BALLDONTLIE GOAT calls. Current player-impact source is box-score-derived only. NBA Data provides current-season play-by-play, possessions and lineup stints; impact metrics remain disabled until schema and calculations are verified. Dunks & Threes EPM remains disabled without written betting-use authorization.",
    }
