"""Read-only adapter for ramirobentes/nba_data Parquet releases.

Current-season play-by-play, possession and lineup-stint schemas are inspected
before derived player/lineup metrics are stored. Raw Parquet is never redistributed
and these early-season estimates do not directly alter game projections.
"""
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone, date
from urllib.request import Request, urlopen

SOURCE = "https://github.com/ramirobentes/nba_data"
SEASON_END_YEAR = 2027
TABLES = ("pbp", "possessions", "lineup_stats")
BASE = "https://github.com/ramirobentes/nba_data/releases/download"
SHRINKAGE_PRIOR_POSSESSIONS = 600.0


def _db():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def _initialize():
    with _db() as conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS nba_data_source_inspections (
            id BIGSERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            season_end_year INTEGER NOT NULL,
            inspected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL,
            details JSONB NOT NULL
          );
          CREATE INDEX IF NOT EXISTS nba_data_source_inspections_latest
            ON nba_data_source_inspections(inspected_at DESC);
          CREATE TABLE IF NOT EXISTS nba_player_lineup_impacts (
            snapshot_date DATE NOT NULL,
            season_end_year INTEGER NOT NULL,
            team TEXT NOT NULL,
            player_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            on_net_rating DOUBLE PRECISION,
            off_net_rating DOUBLE PRECISION,
            raw_on_off DOUBLE PRECISION,
            shrunk_on_off DOUBLE PRECISION,
            on_possessions DOUBLE PRECISION NOT NULL,
            off_possessions DOUBLE PRECISION NOT NULL,
            games INTEGER NOT NULL,
            reliability DOUBLE PRECISION NOT NULL,
            eligible_for_model BOOLEAN NOT NULL DEFAULT FALSE,
            source TEXT NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(snapshot_date,season_end_year,team,player_id)
          );
          CREATE INDEX IF NOT EXISTS nba_player_lineup_impacts_latest
            ON nba_player_lineup_impacts(snapshot_date DESC,team);
          CREATE TABLE IF NOT EXISTS nba_lineup_ratings (
            snapshot_date DATE NOT NULL,
            season_end_year INTEGER NOT NULL,
            team TEXT NOT NULL,
            lineup_key TEXT NOT NULL,
            lineup_players JSONB NOT NULL,
            net_rating DOUBLE PRECISION NOT NULL,
            possessions DOUBLE PRECISION NOT NULL,
            seconds_played DOUBLE PRECISION NOT NULL,
            games INTEGER NOT NULL,
            eligible_for_model BOOLEAN NOT NULL DEFAULT FALSE,
            source TEXT NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(snapshot_date,season_end_year,team,lineup_key)
          );
          CREATE INDEX IF NOT EXISTS nba_lineup_ratings_latest
            ON nba_lineup_ratings(snapshot_date DESC,team);
        """)


def status():
    _initialize()
    with _db() as conn:
        row = conn.execute("""
          SELECT inspected_at,status,details FROM nba_data_source_inspections
          ORDER BY inspected_at DESC LIMIT 1
        """).fetchone()
        impact = conn.execute("""
          SELECT MAX(snapshot_date),
                 COUNT(*) FILTER (WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM nba_player_lineup_impacts)),
                 COUNT(*) FILTER (WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM nba_player_lineup_impacts) AND eligible_for_model),
                 (SELECT COUNT(*) FROM nba_lineup_ratings l
                   WHERE l.snapshot_date=(SELECT MAX(snapshot_date) FROM nba_lineup_ratings))
          FROM nba_player_lineup_impacts
        """).fetchone()
    return {
        "source": SOURCE,
        "season_end_year": SEASON_END_YEAR,
        "season_label": "2026-27",
        "tables": list(TABLES),
        "latest_inspection": (
            {"inspected_at": row[0].isoformat(), "status": row[1], "details": row[2]}
            if row else None
        ),
        "derived_metrics": {
            "snapshot_date": impact[0].isoformat() if impact and impact[0] else None,
            "players": int(impact[1] or 0) if impact else 0,
            "eligible_players": int(impact[2] or 0) if impact else 0,
            "lineups": int(impact[3] or 0) if impact else 0,
            "method": "lineup-stint on/off net rating with 600-possession shrinkage prior",
            "model_influence": "disabled_pending_walk_forward_validation",
        },
        "model_influence": "disabled_pending_walk_forward_validation",
        "license_status": "No explicit repository license was found; raw data is not redistributed.",
    }


def _download(url, path, max_bytes=80_000_000):
    request = Request(url, headers={
        "User-Agent": "ColtsNBAEdge/2.0",
        "Accept": "application/octet-stream",
    })
    total = 0
    with urlopen(request, timeout=30) as response, open(path, "wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError("Parquet source exceeded the 80 MB safety limit.")
            output.write(chunk)
    return total


def _players(lineup):
    """Return validated (NBA person ID, display name) pairs from source lineup text."""
    found = []
    for part in str(lineup or "").split(","):
        match = re.match(r"^\\s*(\\d{5,9})\\s+(.+?)\\s*$", part)
        if match:
            found.append((match.group(1), match.group(2)))
    # Reject incomplete or malformed lineup strings rather than guessing.
    return found if len(found) == 5 and len({item[0] for item in found}) == 5 else []


def _calculate_impacts(connection, parquet_path):
    """Derive transparent, shrinkage-adjusted on/off and five-man lineup metrics."""
    escaped = parquet_path.replace("'", "''")
    cursor = connection.execute(f"""
      SELECT game_id, period, stint, team, lineup_team,
             COALESCE(poss_team,0), COALESCE(pts_team,0),
             COALESCE(poss_opp,0), COALESCE(pts_opp,0),
             COALESCE(secs_played,0)
      FROM read_parquet('{escaped}')
      WHERE team IS NOT NULL AND lineup_team IS NOT NULL
    """)
    team_totals = defaultdict(lambda: {
        "off_poss": 0.0, "off_pts": 0.0, "def_poss": 0.0, "def_pts": 0.0,
        "games": set(), "rows": 0
    })
    player_on = {}
    lineup_totals = {}
    seen = set()
    for row in cursor.fetchall():
        game_id, period, stint, team, lineup, op, pts, dp, dpts, seconds = row
        key = (str(game_id), int(period or 0), int(stint or 0), str(team))
        if key in seen:
            continue
        seen.add(key)
        op, pts, dp, dpts = float(op or 0), float(pts or 0), float(dp or 0), float(dpts or 0)
        seconds = float(seconds or 0)
        if op <= 0 or dp <= 0 or min(pts, dpts) < 0:
            continue
        players = _players(lineup)
        if len(players) != 5:
            continue
        game_id = str(game_id)
        team = str(team)
        total = team_totals[team]
        total["off_poss"] += op
        total["off_pts"] += pts
        total["def_poss"] += dp
        total["def_pts"] += dpts
        total["games"].add(game_id)
        total["rows"] += 1
        for player_id, player_name in players:
            pkey = (team, player_id)
            item = player_on.setdefault(pkey, {
                "name": player_name, "off_poss": 0.0, "off_pts": 0.0,
                "def_poss": 0.0, "def_pts": 0.0, "games": set()
            })
            item["off_poss"] += op
            item["off_pts"] += pts
            item["def_poss"] += dp
            item["def_pts"] += dpts
            item["games"].add(game_id)
        ids = sorted(player_id for player_id, _ in players)
        lkey = (team, "|".join(ids))
        lineup = lineup_totals.setdefault(lkey, {
            "players": [{"id": pid, "name": name} for pid, name in sorted(players)],
            "off_poss": 0.0, "off_pts": 0.0, "def_poss": 0.0, "def_pts": 0.0,
            "seconds": 0.0, "games": set()
        })
        lineup["off_poss"] += op
        lineup["off_pts"] += pts
        lineup["def_poss"] += dp
        lineup["def_pts"] += dpts
        lineup["seconds"] += seconds
        lineup["games"].add(game_id)

    snapshot_date = date.today()
    player_rows = []
    for (team, player_id), item in player_on.items():
        total = team_totals[team]
        on_poss = min(item["off_poss"], item["def_poss"])
        off_off_poss = total["off_poss"] - item["off_poss"]
        off_def_poss = total["def_poss"] - item["def_poss"]
        off_poss = min(off_off_poss, off_def_poss)
        on_net = (100.0 * item["off_pts"] / item["off_poss"] -
                  100.0 * item["def_pts"] / item["def_poss"]) if min(item["off_poss"], item["def_poss"]) > 0 else None
        off_net = (100.0 * (total["off_pts"] - item["off_pts"]) / off_off_poss -
                   100.0 * (total["def_pts"] - item["def_pts"]) / off_def_poss) if min(off_off_poss, off_def_poss) > 0 else None
        if on_net is None or off_net is None:
            continue
        raw = on_net - off_net
        reliability = on_poss / (on_poss + SHRINKAGE_PRIOR_POSSESSIONS) if on_poss > 0 else 0.0
        eligible = on_poss >= 200 and off_poss >= 200 and len(item["games"]) >= 2
        player_rows.append({
            "snapshot_date": snapshot_date, "season_end_year": SEASON_END_YEAR,
            "team": team, "player_id": player_id, "player_name": item["name"],
            "on_net_rating": on_net, "off_net_rating": off_net, "raw_on_off": raw,
            "shrunk_on_off": raw * reliability, "on_possessions": on_poss,
            "off_possessions": off_poss, "games": len(item["games"]),
            "reliability": reliability, "eligible_for_model": eligible, "source": SOURCE
        })

    lineup_rows = []
    for (team, lineup_key), item in lineup_totals.items():
        off_poss = item["off_poss"]
        def_poss = item["def_poss"]
        if min(off_poss, def_poss) <= 0:
            continue
        net = 100.0 * item["off_pts"] / off_poss - 100.0 * item["def_pts"] / def_poss
        possessions = min(off_poss, def_poss)
        games = len(item["games"])
        lineup_rows.append({
            "snapshot_date": snapshot_date, "season_end_year": SEASON_END_YEAR,
            "team": team, "lineup_key": lineup_key, "lineup_players": item["players"],
            "net_rating": net, "possessions": possessions, "seconds_played": item["seconds"],
            "games": games, "eligible_for_model": possessions >= 100 and games >= 2,
            "source": SOURCE
        })

    with _db() as conn:
        conn.cursor().executemany("""
          INSERT INTO nba_player_lineup_impacts
          (snapshot_date,season_end_year,team,player_id,player_name,on_net_rating,off_net_rating,
           raw_on_off,shrunk_on_off,on_possessions,off_possessions,games,reliability,eligible_for_model,source)
          VALUES (%(snapshot_date)s,%(season_end_year)s,%(team)s,%(player_id)s,%(player_name)s,
           %(on_net_rating)s,%(off_net_rating)s,%(raw_on_off)s,%(shrunk_on_off)s,%(on_possessions)s,
           %(off_possessions)s,%(games)s,%(reliability)s,%(eligible_for_model)s,%(source)s)
          ON CONFLICT(snapshot_date,season_end_year,team,player_id) DO UPDATE SET
           player_name=EXCLUDED.player_name,on_net_rating=EXCLUDED.on_net_rating,
           off_net_rating=EXCLUDED.off_net_rating,raw_on_off=EXCLUDED.raw_on_off,
           shrunk_on_off=EXCLUDED.shrunk_on_off,on_possessions=EXCLUDED.on_possessions,
           off_possessions=EXCLUDED.off_possessions,games=EXCLUDED.games,
           reliability=EXCLUDED.reliability,eligible_for_model=EXCLUDED.eligible_for_model,
           source=EXCLUDED.source,captured_at=NOW()
        """, player_rows)
        conn.cursor().executemany("""
          INSERT INTO nba_lineup_ratings
          (snapshot_date,season_end_year,team,lineup_key,lineup_players,net_rating,possessions,
           seconds_played,games,eligible_for_model,source)
          VALUES (%(snapshot_date)s,%(season_end_year)s,%(team)s,%(lineup_key)s,%(lineup_players)s::jsonb,
           %(net_rating)s,%(possessions)s,%(seconds_played)s,%(games)s,%(eligible_for_model)s,%(source)s)
          ON CONFLICT(snapshot_date,season_end_year,team,lineup_key) DO UPDATE SET
           lineup_players=EXCLUDED.lineup_players,net_rating=EXCLUDED.net_rating,
           possessions=EXCLUDED.possessions,seconds_played=EXCLUDED.seconds_played,
           games=EXCLUDED.games,eligible_for_model=EXCLUDED.eligible_for_model,
           source=EXCLUDED.source,captured_at=NOW()
        """, [{**row, "lineup_players": json.dumps(row["lineup_players"])} for row in lineup_rows])

    eligible_players = sum(1 for row in player_rows if row["eligible_for_model"])
    eligible_lineups = sum(1 for row in lineup_rows if row["eligible_for_model"])
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "teams": len(team_totals),
        "players": len(player_rows),
        "eligible_players": eligible_players,
        "lineups": len(lineup_rows),
        "eligible_lineups": eligible_lineups,
        "team_rows": sum(row["rows"] for row in team_totals.values()),
        "method": "On-court net rating minus off-court net rating, shrunk toward zero using on-court possessions / (on-court possessions + 600).",
        "eligibility": "Player model eligibility requires at least 200 on-court and 200 off-court possessions across at least two games. Lineup eligibility requires at least 100 possessions across at least two games.",
        "model_influence": "disabled_pending_walk_forward_validation",
    }


def inspect_current_season():
    """Download the three 2026-27 assets, inspect schemas, and derive on/off metrics."""
    import duckdb

    _initialize()
    details = {
        "source": SOURCE,
        "season_end_year": SEASON_END_YEAR,
        "season_label": "2026-27",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
        "model_influence": "disabled_pending_walk_forward_validation",
    }
    status_value = "complete"
    with tempfile.TemporaryDirectory(prefix="nba-data-") as temp_dir:
        connection = duckdb.connect(database=":memory:")
        try:
            for table in TABLES:
                filename = f"{table}_regular_{SEASON_END_YEAR}.parquet"
                url = f"{BASE}/{table}/{filename}"
                path = os.path.join(temp_dir, filename)
                size = _download(url, path)
                escaped = path.replace("'", "''")
                schema_rows = connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
                ).fetchall()
                columns = [{"name": row[0], "type": row[1]} for row in schema_rows]
                count = connection.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{escaped}')"
                ).fetchone()[0]
                cursor = connection.execute(
                    f"SELECT * FROM read_parquet('{escaped}') LIMIT 2"
                )
                sample = [
                    dict(zip([item[0] for item in cursor.description], row))
                    for row in cursor.fetchall()
                ]
                sample = json.loads(json.dumps(sample, default=str))
                details["tables"][table] = {
                    "file": filename,
                    "download_bytes": size,
                    "row_count": int(count),
                    "columns": columns,
                    "sample_rows": sample,
                    "source_url": url,
                }
                if table == "lineup_stats":
                    details["derived_player_lineup_metrics"] = _calculate_impacts(connection, path)
        except Exception as exc:
            status_value = "partial"
            details["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        finally:
            connection.close()

    with _db() as conn:
        conn.execute("""
          INSERT INTO nba_data_source_inspections(source,season_end_year,status,details)
          VALUES (%s,%s,%s,%s::jsonb)
        """, (SOURCE, SEASON_END_YEAR, status_value, json.dumps(details, default=str)))
    return {"success": status_value == "complete", "status": status_value, **details}
