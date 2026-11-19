"""Strict, timestamped pregame feature contract for the Benter NBA Points model.

Every field is required. Missing data must be sourced or the observation is
excluded; it is never replaced with a made-up league average or zero.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any


POINTS_FEATURE_NAMES = (
    "projected_minutes",
    "points_per_minute_l5",
    "points_per_minute_l10",
    "points_per_minute_season",
    "usage_rate",
    "usage_rate_delta",
    "team_pace",
    "opponent_pace",
    "opponent_def_rating",
    "position_points_allowed_z",
    "teammate_usage_vacated",
    "rest_days",
    "home",
    "back_to_back",
    "projection_minus_line",
)

# Plausibility bounds catch unit mistakes and corrupted feeds, not model opinions.
BOUNDS = {
    "projected_minutes": (0.0, 60.0),
    "points_per_minute_l5": (0.0, 1.8),
    "points_per_minute_l10": (0.0, 1.8),
    "points_per_minute_season": (0.0, 1.8),
    "usage_rate": (0.0, 1.0),
    "usage_rate_delta": (-0.5, 0.5),
    "team_pace": (60.0, 130.0),
    "opponent_pace": (60.0, 130.0),
    "opponent_def_rating": (80.0, 140.0),
    "position_points_allowed_z": (-5.0, 5.0),
    "teammate_usage_vacated": (0.0, 0.8),
    "rest_days": (0.0, 14.0),
    "home": (0.0, 1.0),
    "back_to_back": (0.0, 1.0),
    "projection_minus_line": (-30.0, 30.0),
}


def _aware_datetime(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed


def build_points_feature_vector(snapshot: dict[str, Any]) -> list[float]:
    """Validate provenance/timing and return the fixed-order numeric vector.

    Required metadata:
      feature_as_of: timezone-aware timestamp when the snapshot was frozen
      tipoff_at: timezone-aware scheduled tipoff timestamp
      source_status: "verified"

    Each numeric feature must be present and within its declared plausibility
    bounds. The caller must only set source_status=verified after checking the
    underlying official/market source records.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Feature snapshot must be an object.")
    if snapshot.get("source_status") != "verified":
        raise ValueError("Feature snapshot is not marked verified.")
    as_of = _aware_datetime(snapshot.get("feature_as_of"), "feature_as_of")
    tipoff = _aware_datetime(snapshot.get("tipoff_at"), "tipoff_at")
    if as_of >= tipoff:
        raise ValueError("Feature snapshot must be frozen strictly before tipoff.")

    vector = []
    for name in POINTS_FEATURE_NAMES:
        if name not in snapshot or snapshot[name] is None:
            raise ValueError(f"Missing required pregame feature: {name}")
        try:
            value = float(snapshot[name])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Feature {name} must be numeric.") from exc
        if not math.isfinite(value):
            raise ValueError(f"Feature {name} must be finite.")
        lower, upper = BOUNDS[name]
        if not lower <= value <= upper:
            raise ValueError(f"Feature {name}={value} is outside [{lower}, {upper}].")
        vector.append(value)
    return vector
