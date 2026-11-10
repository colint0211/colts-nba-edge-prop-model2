from datetime import datetime
from zoneinfo import ZoneInfo


def candidate_is_pregame(prop, slate_date, now_et):
    """Accept only a timezone-aware future tipoff on the exact Eastern slate date."""
    if not isinstance(prop, dict):
        return False
    raw_start = (
        prop.get("game_start") or prop.get("commence_time")
        or prop.get("start_time") or prop.get("game_time")
    )
    if not raw_start:
        return False
    try:
        start = datetime.fromisoformat(str(raw_start).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if start.tzinfo is None or start.utcoffset() is None:
        return False
    start_et = start.astimezone(ZoneInfo("America/New_York"))
    return start_et.date().isoformat() == str(slate_date) and start_et > now_et
