#!/usr/bin/env python3
"""Fetch the newest parseable official NBA injury report using nbainjuries.

The report is published on a separate Git branch so frequent injury refreshes
do not trigger Vercel production deployments.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nbainjuries import injury

VALID_STATUSES = {
    "available", "probable", "questionable", "doubtful", "out",
    "suspension", "suspended", "not with team", "inactive",
}


def clean_records(frame):
    if frame is None or not hasattr(frame, "to_dict"):
        return []
    rows = frame.fillna("").to_dict(orient="records")
    cleaned = []
    for row in rows:
        name = str(row.get("Player Name") or "").strip()
        team = str(row.get("Team") or "").strip()
        status = str(row.get("Current Status") or "").strip()
        reason = str(row.get("Reason") or "").strip()
        if not name or not team or not status:
            continue
        if status.casefold() in {"not yet submitted", "nan", "none"}:
            continue
        # Keep unfamiliar non-empty official status labels, but reject obvious
        # PDF extraction artifacts and rows without a player/team identity.
        if len(name) > 80 or len(team) > 80 or len(status) > 40:
            continue
        cleaned.append({
            "game_date": str(row.get("Game Date") or "").strip(),
            "game_time": str(row.get("Game Time") or "").strip(),
            "matchup": str(row.get("Matchup") or "").strip(),
            "team": team,
            "player_name": name,
            "status": status,
            "reason": reason,
        })
    return cleaned


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "nba_injuries_latest.json")
    now_et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None, second=0, microsecond=0)
    minute = (now_et.minute // 15) * 15
    latest_slot = now_et.replace(minute=minute)
    latest_slot = min(latest_slot, now_et)

    # Reports since the new 15-minute naming convention began. Probe newest
    # first; a missing or unparsable PDF never replaces the last good snapshot.
    for step in range(0, 145):
        candidate = latest_slot - timedelta(minutes=15 * step)
        if candidate < datetime(2025, 12, 22, 9, 0):
            break
        try:
            if not injury.check_reportvalid(candidate):
                continue
            frame = injury.get_reportdata(candidate, return_df=True)
            records = clean_records(frame)
            if not records:
                continue
            known = sum(1 for row in records if row["status"].casefold() in VALID_STATUSES)
            if known == 0:
                continue
            payload = {
                "schema_version": 1,
                "source": "NBA official team injury reports via nbainjuries 1.1.1",
                "report_timestamp_et": candidate.isoformat(timespec="minutes"),
                "fetched_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
                "season": "2026-27",
                "record_count": len(records),
                "records": records,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            print(f"Published {len(records)} official injury rows from {candidate.isoformat(timespec='minutes')} ET.")
            return 0
        except Exception as exc:
            print(f"Could not parse {candidate.isoformat(timespec='minutes')} ET: {type(exc).__name__}: {exc}")
            continue

    print("No valid official injury report was found in the last 36 hours; keeping the previous published snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
