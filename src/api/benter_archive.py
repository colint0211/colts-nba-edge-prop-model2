"""Research-only archive for exact DraftKings NBA player-prop quotes.

This module stores every available DK main-line Points/Threes quote with both
Over and Under prices at the same line. It does not create picks or touch the
production prediction ledger.
"""
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _connect():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def _safe_price(value):
    try:
        price = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if price == 0 or abs(price) > 10000:
        return None
    return price


def _event_start(event):
    for key in ("commence_time", "start_time", "scheduled_at", "date"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_dk_quotes(events):
    markets = {"points": "player_points", "threes": "player_threes"}
    quotes = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            continue
        away = str(event.get("away_team") or "")
        home = str(event.get("home_team") or "")
        matchup = (away + " @ " + home).strip(" @")
        for bookmaker in event.get("bookmakers") or []:
            if not isinstance(bookmaker, dict):
                continue
            if str(bookmaker.get("key") or "").strip().lower() != "draftkings":
                continue
            for market in bookmaker.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                market_name = next(
                    (name for name, key in markets.items() if market.get("key") == key),
                    None,
                )
                if not market_name or market.get("suspended_at"):
                    continue
                line_type = str(market.get("line_type") or "").lower()
                if line_type and line_type != "main":
                    continue
                grouped = {}
                for outcome in market.get("outcomes") or []:
                    if not isinstance(outcome, dict):
                        continue
                    side = str(outcome.get("name") or "").strip().lower()
                    if side not in ("over", "under"):
                        continue
                    player = str(
                        outcome.get("description")
                        or outcome.get("player")
                        or market.get("player")
                        or ""
                    ).strip()
                    if not player:
                        continue
                    team = ""
                    match = re.search(r"\(([A-Z]{2,3})\)\s*$", player)
                    if match:
                        team = match.group(1)
                        player = player[:match.start()].strip()
                    try:
                        line = float(outcome.get("point"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    price = _safe_price(outcome.get("price"))
                    if not math.isfinite(line) or price is None:
                        continue
                    key = (player.casefold(), line)
                    row = grouped.setdefault(key, {
                        "player": player,
                        "team": team,
                        "line": line,
                        "over_odds": None,
                        "under_odds": None,
                    })
                    row[side + "_odds"] = price
                for row in grouped.values():
                    if row["over_odds"] is None or row["under_odds"] is None:
                        continue
                    quotes.append({
                        "event_id": event_id,
                        "event": matchup,
                        "event_start": _event_start(event),
                        "market": market_name,
                        **row,
                        "bookmaker": "DraftKings",
                        "market_last_update": market.get("last_update"),
                    })
    return quotes


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nba_benter_market_archive (
          id BIGSERIAL PRIMARY KEY,
          quote_fingerprint TEXT NOT NULL UNIQUE,
          event_id TEXT NOT NULL,
          event TEXT,
          event_start TIMESTAMPTZ,
          market TEXT NOT NULL CHECK (market IN ('points','threes')),
          player TEXT NOT NULL,
          player_key TEXT NOT NULL,
          team TEXT,
          line DOUBLE PRECISION NOT NULL,
          over_odds INTEGER NOT NULL,
          under_odds INTEGER NOT NULL,
          no_vig_over DOUBLE PRECISION NOT NULL,
          no_vig_under DOUBLE PRECISION NOT NULL,
          fetched_at TIMESTAMPTZ NOT NULL,
          market_last_update TIMESTAMPTZ,
          raw_quote JSONB NOT NULL DEFAULT '{}'::jsonb,
          actual_value DOUBLE PRECISION,
          outcome TEXT,
          settled_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE nba_benter_market_archive
          ADD COLUMN IF NOT EXISTS actual_value DOUBLE PRECISION;
        ALTER TABLE nba_benter_market_archive
          ADD COLUMN IF NOT EXISTS outcome TEXT;
        ALTER TABLE nba_benter_market_archive
          ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;
        CREATE INDEX IF NOT EXISTS nba_benter_archive_event_market
          ON nba_benter_market_archive(event_id, market, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS nba_benter_archive_player_date
          ON nba_benter_market_archive(player_key, market, fetched_at DESC);
    """)


@router.get("/api/research/benter/archive/capture")
def capture_draftkings_quotes():
    """Fetch and append current DK main-line quotes; safe to call repeatedly."""
    try:
        # Lazy import avoids a module cycle while reusing the app's configured
        # PropLine client, API-key handling, cooldown and cached response.
        from . import index as app_module
        events, source, headers = app_module._propline_fetch_events()
        quotes = _extract_dk_quotes(events)
        fetched_at_text = (headers or {}).get("fetched_at")
        fetched_at = (
            datetime.fromisoformat(fetched_at_text.replace("Z", "+00:00"))
            if fetched_at_text else datetime.now(timezone.utc)
        )
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)

        inserted = 0
        with _connect() as conn:
            _ensure_table(conn)
            for quote in quotes:
                over_raw = 100.0 / (quote["over_odds"] + 100.0) if quote["over_odds"] > 0 else abs(quote["over_odds"]) / (abs(quote["over_odds"]) + 100.0)
                under_raw = 100.0 / (quote["under_odds"] + 100.0) if quote["under_odds"] > 0 else abs(quote["under_odds"]) / (abs(quote["under_odds"]) + 100.0)
                total = over_raw + under_raw
                if total <= 0:
                    continue
                start = quote.get("event_start")
                try:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
                    if start_dt and start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    start_dt = None
                updated = quote.get("market_last_update")
                try:
                    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else None
                    if updated_dt and updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    updated_dt = None
                player_key = " ".join(quote["player"].casefold().split())
                identity = "|".join([
                    quote["event_id"], quote["market"], player_key,
                    format(quote["line"], ".4f"), str(quote["over_odds"]),
                    str(quote["under_odds"]), fetched_at.isoformat(),
                ])
                fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                cur = conn.execute("""
                    INSERT INTO nba_benter_market_archive
                      (quote_fingerprint,event_id,event,event_start,market,player,
                       player_key,team,line,over_odds,under_odds,no_vig_over,
                       no_vig_under,fetched_at,market_last_update,raw_quote)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (quote_fingerprint) DO NOTHING
                """, (
                    fingerprint, quote["event_id"], quote["event"], start_dt,
                    quote["market"], quote["player"], player_key, quote["team"],
                    quote["line"], quote["over_odds"], quote["under_odds"],
                    over_raw / total, under_raw / total, fetched_at, updated_dt,
                    json.dumps(quote, separators=(",", ":"), allow_nan=False),
                ))
                inserted += max(0, cur.rowcount)
            summary = conn.execute("""
                SELECT COUNT(*), COUNT(DISTINCT event_id), COUNT(DISTINCT player_key),
                       MIN(fetched_at), MAX(fetched_at)
                FROM nba_benter_market_archive
            """).fetchone()

        return {
            "status": "captured" if inserted else "no_new_quotes",
            "source": source,
            "source_fetched_at": fetched_at.isoformat(),
            "events_received": len(events or []),
            "dk_main_line_quotes_found": len(quotes),
            "new_quote_snapshots_saved": inserted,
            "archive": {
                "total_snapshots": summary[0],
                "distinct_events": summary[1],
                "distinct_players": summary[2],
                "first_capture": summary[3].isoformat() if summary[3] else None,
                "last_capture": summary[4].isoformat() if summary[4] else None,
            },
            "scope": "DraftKings only; Points and Threes; exact main line with both Over and Under prices.",
            "important": "This is a forward-looking archive. It does not recreate missing historical odds or imply that any archived line is a closing line.",
            "quota": {
                "daily_limit": (headers or {}).get("daily_limit"),
                "daily_used": (headers or {}).get("daily_used"),
                "daily_remaining": (headers or {}).get("daily_remaining"),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="DraftKings archive capture failed: " + type(exc).__name__)


@router.get("/api/research/benter/archive/status")
def draftkings_archive_status():
    try:
        with _connect() as conn:
            _ensure_table(conn)
            row = conn.execute("""
                SELECT COUNT(*), COUNT(DISTINCT event_id), COUNT(DISTINCT player_key),
                       COUNT(*) FILTER (WHERE market='points'),
                       COUNT(*) FILTER (WHERE market='threes'),
                       MIN(fetched_at), MAX(fetched_at)
                FROM nba_benter_market_archive
            """).fetchone()
            dates = conn.execute("""
                SELECT fetched_at::date::text AS capture_date, COUNT(*)
                FROM nba_benter_market_archive
                GROUP BY fetched_at::date ORDER BY fetched_at::date DESC LIMIT 14
            """).fetchall()
        return {
            "total_snapshots": row[0],
            "distinct_events": row[1],
            "distinct_players": row[2],
            "points_snapshots": row[3],
            "threes_snapshots": row[4],
            "first_capture": row[5].isoformat() if row[5] else None,
            "last_capture": row[6].isoformat() if row[6] else None,
            "captures_by_date": [{"date": d, "snapshots": n} for d, n in dates],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="DraftKings archive status unavailable: " + type(exc).__name__)


@router.get("/api/research/benter/archive/settle")
def settle_completed_draftkings_quotes():
    """Attach official ESPN game-log outcomes to archived DK quote snapshots."""
    try:
        from zoneinfo import ZoneInfo
        from . import index as app_module
        eastern = ZoneInfo("America/New_York")
        with _connect() as conn:
            _ensure_table(conn)
            rows = conn.execute("""
                SELECT event_id,event,event_start,market,player,player_key,team,line
                FROM nba_benter_market_archive
                WHERE actual_value IS NULL AND event_start IS NOT NULL
                  AND event_start < NOW()
                GROUP BY event_id,event,event_start,market,player,player_key,team,line
                ORDER BY event_start ASC
                LIMIT 500
            """).fetchall()

        grouped_results = {}
        skipped = []
        for event_id,event,event_start,market,player,player_key,team,line in rows:
            game_date = event_start.astimezone(eastern).date().isoformat()
            event_text = str(event or "").lower()
            teams = {
                code for code, team_name in app_module._TEAM_NAMES.items()
                if re.search(r"\b" + re.escape(str(team_name).lower()) + r"\b", event_text)
            }
            if len(teams) != 2:
                skipped.append({
                    "event_id":event_id,"player":player,"reason":"unverified_matchup",
                    "event":str(event or ""),"resolved_teams":sorted(teams),
                })
                continue
            athlete_id, roster_team = app_module._espn_roster_player_id(player, teams)
            if not athlete_id or (team and roster_team and team.upper() != roster_team.upper()):
                skipped.append({"event_id":event_id,"player":player,"reason":"player_identity_not_verified"})
                continue
            field,label = app_module.STAT_FIELDS[market]
            records = app_module._gamelog_records(athlete_id, field, label, 2027)
            matching = [
                item for item in records
                if str(item.get("date") or "")[:10] == game_date
                and isinstance(item.get("value"), (int,float))
                and math.isfinite(float(item["value"]))
            ]
            if len(matching) != 1:
                skipped.append({"event_id":event_id,"player":player,"reason":"no_unique_official_game_log"})
                continue
            actual = float(matching[0]["value"])
            if math.isclose(actual, float(line), rel_tol=0.0, abs_tol=1e-9):
                outcome = "PUSH"
            else:
                outcome = "OVER" if actual > float(line) else "UNDER"
            grouped_results[(event_id,market,player_key,float(line))] = (actual,outcome)

        updated = 0
        with _connect() as conn:
            _ensure_table(conn)
            for (event_id,market,player_key,line),(actual,outcome) in grouped_results.items():
                cur = conn.execute("""
                    UPDATE nba_benter_market_archive
                    SET actual_value=%s,outcome=%s,settled_at=NOW()
                    WHERE event_id=%s AND market=%s AND player_key=%s
                      AND line=%s AND actual_value IS NULL
                """,(actual,outcome,event_id,market,player_key,line))
                updated += max(0,cur.rowcount)
            totals = conn.execute("""
                SELECT COUNT(*),COUNT(*) FILTER (WHERE actual_value IS NOT NULL),
                       COUNT(DISTINCT event_id) FILTER (WHERE actual_value IS NOT NULL)
                FROM nba_benter_market_archive
            """).fetchone()
        return {
            "status":"settled" if updated else "no_new_settlements",
            "unique_player_lines_checked":len(rows),
            "unique_player_lines_resolved":len(grouped_results),
            "quote_snapshots_updated":updated,
            "skipped_count":len(skipped),
            "archive":{"total_quote_snapshots":totals[0],"settled_quote_snapshots":totals[1],
                       "events_with_settlements":totals[2]},
            "skipped_examples":skipped[:20],
            "settlement_source":"ESPN regular-season player game logs; exact roster identity and Eastern-local game-date match required.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503,detail="DraftKings archive settlement failed: "+type(exc).__name__)
