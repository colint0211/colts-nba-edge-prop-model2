"""Read-only adapter for ramirobentes/nba_data Parquet releases.

The current 2026-27 season is season-end year 2027. This module inspects the
play-by-play, possession and lineup-stint schemas before any data can influence
the sides/totals model. It never infers impact from unknown column names.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from urllib.request import Request, urlopen

SOURCE = "https://github.com/ramirobentes/nba_data"
SEASON_END_YEAR = 2027
TABLES = ("pbp", "possessions", "lineup_stats")
BASE = "https://github.com/ramirobentes/nba_data/releases/download"


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
        """)


def status():
    _initialize()
    with _db() as conn:
        row = conn.execute("""
          SELECT inspected_at,status,details FROM nba_data_source_inspections
          ORDER BY inspected_at DESC LIMIT 1
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
        "model_influence": "disabled_until_schema_and_metric_validation",
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


def inspect_current_season():
    """Download the three 2026-27 assets and inspect schemas with DuckDB."""
    import duckdb

    _initialize()
    details = {
        "source": SOURCE,
        "season_end_year": SEASON_END_YEAR,
        "season_label": "2026-27",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
        "model_influence": "disabled_until_schema_and_metric_validation",
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
                details["tables"][table] = {
                    "file": filename,
                    "download_bytes": size,
                    "row_count": int(count),
                    "columns": columns,
                    "sample_rows": sample,
                    "source_url": url,
                }
        except Exception as exc:
            status_value = "partial"
            details["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            connection.close()

    with _db() as conn:
        conn.execute("""
          INSERT INTO nba_data_source_inspections(source,season_end_year,status,details)
          VALUES (%s,%s,%s,%s::jsonb)
        """, (SOURCE, SEASON_END_YEAR, status_value, json.dumps(details, default=str)))
    return {"success": status_value == "complete", "status": status_value, **details}
