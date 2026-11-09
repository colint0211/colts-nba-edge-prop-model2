"""Smoke-test the official NBA historical injury-report PDF archive.

The source stores timestamped reports on NBA's public static CDN. This test
checks a known 2024-25 report and confirms that the parser returns structured
player statuses rather than inferring availability from box scores.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from nbainjuries import injury


def main() -> None:
    # 5:30 PM ET request resolves to the official 5 PM snapshot.
    timestamp = datetime(2025, 4, 25, 17, 30)
    if not injury.check_reportvalid(timestamp):
        raise RuntimeError("Official NBA injury report snapshot is unavailable")

    raw = injury.get_reportdata(timestamp)
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Official injury report parsed as an empty result")

    statuses = Counter(str(row.get("Current Status", "")).strip() for row in rows)
    required = {"Game Date", "Game Time", "Matchup", "Team", "Player Name", "Current Status"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise RuntimeError(f"Parsed injury report missing required fields: {missing}")

    print(json.dumps({
        "source": "NBA official injury report PDF archive",
        "requested_snapshot_et": timestamp.isoformat(),
        "rows": len(rows),
        "status_counts": dict(statuses),
        "sample_rows": rows[:5],
        "historical_coverage_documented_from": "2021-22",
        "data_boundary": "This is a timestamped pregame report snapshot; use only snapshots at or before each game's tipoff.",
    }, indent=2))


if __name__ == "__main__":
    main()
