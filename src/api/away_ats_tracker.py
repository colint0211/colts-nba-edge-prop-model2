"""Leakage-safe daily pre-tipoff market capture for away-only ATS research.

This collector stores timestamped sportsbook quotes only. It never creates picks,
changes the production model, or treats a retrieved closing line as a live
observation when its source timestamp is missing, stale, or after tipoff.
"""
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from fastapi import APIRouter, HTTPException, Request as FastRequest
from fastapi.responses import JSONResponse

router = APIRouter()
ET = ZoneInfo("America/New_York")
BASE = "https://mcp.dunkapi.com/v1/get_odds"
MAX_CAPTURE_AGE = timedelta(hours=26)


def _db():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL or POSTGRES_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def _init():
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nba_away_ats_market_captures (
          id BIGSERIAL PRIMARY KEY,
          game_id TEXT NOT NULL,
          game_date DATE NOT NULL,
          starts_at TIMESTAMPTZ NOT NULL,
          captured_at TIMESTAMPTZ NOT NULL,
          retrieved_at TIMESTAMPTZ NOT NULL,
          home_team TEXT,
          away_team TEXT,
          provider TEXT NOT NULL,
          home_spread_open DOUBLE PRECISION,
          home_spread_observed DOUBLE PRECISION,
          total_open DOUBLE PRECISION,
          total_observed DOUBLE PRECISION,
          home_ml_open DOUBLE PRECISION,
          home_ml_observed DOUBLE PRECISION,
          away_ml_open DOUBLE PRECISION,
          away_ml_observed DOUBLE PRECISION,
          spread_home_odds DOUBLE PRECISION,
          spread_away_odds DOUBLE PRECISION,
          raw_payload JSONB NOT NULL,
          UNIQUE(game_id, provider, captured_at)
        );
        CREATE INDEX IF NOT EXISTS nba_away_ats_capture_game
          ON nba_away_ats_market_captures(game_id, captured_at);
        CREATE INDEX IF NOT EXISTS nba_away_ats_capture_date
          ON nba_away_ats_market_captures(game_date, starts_at);
        """)


def _parse_timestamp(value):
    """Return an aware UTC datetime; reject absent, malformed, or naive timestamps."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _dunk_post(api_key, date_et):
    body = json.dumps({"date": date_et, "league": "nba"}).encode("utf-8")
    req = Request(BASE, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-key": api_key,
        "User-Agent": "ColtsNBAEdge/1.0",
    })
    try:
        with urlopen(req, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raise RuntimeError("DunkAPI get_odds returned HTTP " + str(exc.code)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("DunkAPI returned an unexpected response.")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    games = payload.get("games")
    if not isinstance(games, list):
        raise RuntimeError("DunkAPI response did not contain games[].")
    return games


def _float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


@router.get("/api/away-ats/collect")
def collect_away_ats_market_capture(request: FastRequest):
    """Daily Vercel Cron: store only verified, timestamped pre-tipoff quotes."""
    expected = os.getenv("CRON_SECRET", "").strip()
    supplied = request.headers.get("authorization", "")
    if not expected or not secrets.compare_digest(supplied, "Bearer " + expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    api_key = os.getenv("DUNKAPI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="DUNKAPI_API_KEY is not configured in this Vercel environment.")

    now = datetime.now(timezone.utc)
    date_et = now.astimezone(ET).date().isoformat()
    try:
        games = _dunk_post(api_key, date_et)
        _init()
        inserted = 0
        skipped_started = 0
        skipped_timestamp = 0
        skipped_stale = 0
        skipped_missing = 0
        with _db() as conn:
            for game in games:
                if not isinstance(game, dict):
                    continue
                game_id = str(game.get("game_id") or "").strip()
                starts = _parse_timestamp(game.get("starts_at"))
                if not game_id or starts is None:
                    skipped_missing += 1
                    continue
                if starts <= now:
                    skipped_started += 1
                    continue

                home = game.get("home") or {}
                away = game.get("away") or {}
                books = game.get("books") or []
                if not isinstance(books, list) or not books:
                    skipped_missing += 1
                    continue

                game_date = game.get("date") or date_et
                for book in books:
                    if not isinstance(book, dict) or not book.get("provider"):
                        skipped_missing += 1
                        continue

                    # Never substitute retrieval time: captured_at must be the
                    # sportsbook quote's own timestamp and must precede tipoff.
                    captured = _parse_timestamp(book.get("captured_at"))
                    if captured is None or captured > now or captured >= starts:
                        skipped_timestamp += 1
                        continue
                    if now - captured > MAX_CAPTURE_AGE:
                        skipped_stale += 1
                        continue

                    # DunkAPI exposes its current/closing spread in this field.
                    # Store it as an observed pre-tipoff quote, not as a
                    # guaranteed final close; preserve the source timestamp.
                    observed_spread = _float(book.get("home_spread_close"))
                    if observed_spread is None:
                        skipped_missing += 1
                        continue

                    provider = str(book.get("provider")).strip()
                    values = (
                        game_id, game_date, starts, captured, now,
                        home.get("abbrev"), away.get("abbrev"), provider,
                        _float(book.get("home_spread_open")), observed_spread,
                        _float(book.get("total_open")), _float(book.get("total_close")),
                        _float(book.get("home_ml_open")), _float(book.get("home_ml_close")),
                        _float(book.get("away_ml_open")), _float(book.get("away_ml_close")),
                        _float(book.get("spread_home_odds")), _float(book.get("spread_away_odds")),
                        json.dumps({
                            "game": game,
                            "book": book,
                            "capture_semantics": "timestamped pre-tipoff quote; home_spread_close source field stored as observed, not asserted to be final close"
                        }, default=str),
                    )
                    cur = conn.execute("""
                      INSERT INTO nba_away_ats_market_captures
                        (game_id,game_date,starts_at,captured_at,retrieved_at,home_team,away_team,provider,
                         home_spread_open,home_spread_observed,total_open,total_observed,
                         home_ml_open,home_ml_observed,away_ml_open,away_ml_observed,
                         spread_home_odds,spread_away_odds,raw_payload)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                      ON CONFLICT(game_id,provider,captured_at) DO NOTHING
                    """, values)
                    inserted += max(0, cur.rowcount or 0)

        return JSONResponse({
            "status": "captured",
            "source": "DunkAPI get_odds",
            "date_et": date_et,
            "retrieved_at_utc": now.isoformat(),
            "games_returned": len(games),
            "book_snapshots_inserted": inserted,
            "games_already_started_skipped": skipped_started,
            "snapshots_missing_or_invalid_timestamp_skipped": skipped_timestamp,
            "snapshots_older_than_26h_skipped": skipped_stale,
            "games_or_books_missing_spread_data_skipped": skipped_missing,
            "model_picks_created": 0,
            "note": "Research-only timestamped pre-tipoff quotes. No picks are generated; source quotes are not assumed to be final closing lines."
        })
    except Exception as exc:
        message = str(exc).replace(api_key, "[redacted]")
        return JSONResponse({
            "status": "unavailable",
            "error": type(exc).__name__,
            "detail": message[:240]
        }, status_code=503)
