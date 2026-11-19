"""Read-only, isolated Benter pilot backtest over the existing NBA points ledger.

This endpoint is intended only for a Vercel preview deployment. It reads settled
points picks, reconstructs a strict pregame feature vector from each immutable
snapshot, and performs a date-grouped two-stage walk-forward test. It never
writes to Neon or changes production picks.
"""
import json
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from .benter_model import (
    expected_value_units,
    probability_metrics,
    walk_forward_benter,
)

router = APIRouter()
ET = ZoneInfo("America/New_York")
FEATURES = (
    "signed_projection_edge",
    "signed_recent5_edge",
    "signed_recent10_edge",
    "signed_season_edge",
    "projected_minutes",
    "distribution_std_dev",
    "minute_adjustment_factor",
    "games_used",
)


def _as_float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _snapshot(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _ledger_rows():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured for this preview.")
    import psycopg
    with psycopg.connect(url, connect_timeout=8, options="-c default_transaction_read_only=on") as conn:
        return conn.execute(
            """
            SELECT id, player, event, event_id, line, side, odds,
                   model_probability, market_probability, actual_value,
                   outcome, created_at, snapshot
            FROM nba_predictions
            WHERE market='points'
              AND settled_at IS NOT NULL
              AND outcome IN ('WIN','LOSS')
              AND model_probability IS NOT NULL
              AND market_probability IS NOT NULL
              AND odds IS NOT NULL
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()


def _make_dataset(db_rows):
    dataset = []
    skipped = []
    source_dates = {}
    for row in db_rows:
        (prediction_id, player, event, event_id, line, side, odds,
         old_model_p, market_p, actual, outcome, created_at, raw_snapshot) = row
        snap = _snapshot(raw_snapshot)
        side = str(side or "").upper()
        direction = 1.0 if side == "OVER" else -1.0 if side == "UNDER" else None
        if direction is None:
            skipped.append({"id": prediction_id, "reason": "invalid_side"})
            continue
        line = _as_float(line)
        projection = _as_float(snap.get("projection"))
        recent5 = _as_float(snap.get("recent5_mean"))
        recent10 = _as_float(snap.get("recent10_mean"))
        season_mean = _as_float(snap.get("long_run_mean"))
        minutes = _as_float(snap.get("projected_minutes"))
        sd = _as_float(snap.get("distribution_std_dev"))
        minute_factor = _as_float(snap.get("minute_adjustment_factor"))
        games_used = _as_float(snap.get("games_used"))
        market_p = _as_float(market_p)
        old_model_p = _as_float(old_model_p)
        required = [line, projection, recent5, recent10, season_mean, minutes,
                    sd, minute_factor, games_used, market_p, old_model_p]
        if any(value is None for value in required):
            skipped.append({"id": prediction_id, "reason": "missing_required_snapshot_feature"})
            continue
        if not 0 < market_p < 1 or not 0 <= old_model_p <= 1:
            skipped.append({"id": prediction_id, "reason": "invalid_probability"})
            continue
        slate_date = snap.get("slate_date")
        if not slate_date:
            slate_date = created_at.astimezone(ET).date().isoformat()
        try:
            date_value = datetime.fromisoformat(str(slate_date)[:10]).date().isoformat()
        except ValueError:
            skipped.append({"id": prediction_id, "reason": "invalid_slate_date"})
            continue
        features = [
            (projection - line) * direction,
            (recent5 - line) * direction,
            (recent10 - line) * direction,
            (season_mean - line) * direction,
            minutes, sd, minute_factor, games_used,
        ]
        target = 1 if outcome == "WIN" else 0
        index = len(dataset)
        dataset.append({
            "date": date_value,
            "features": features,
            "market_probability": market_p,
            "target": target,
        })
        source_dates[index] = {
            "prediction_id": int(prediction_id),
            "player": str(player),
            "event": str(event or ""),
            "event_id": str(event_id or ""),
            "line": line,
            "side": side,
            "odds": int(odds),
            "old_model_probability": old_model_p,
            "market_probability": market_p,
            "actual_value": _as_float(actual),
            "outcome": str(outcome),
            "slate_date": date_value,
            "market_books": snap.get("market_books") or [],
            "best_over_book": snap.get("best_over_book"),
            "best_under_book": snap.get("best_under_book"),
        }
    return dataset, source_dates, skipped


def _brier_for(rows, key):
    if not rows:
        return None
    return round(sum((float(r[key]) - int(r["target"])) ** 2 for r in rows) / len(rows), 6)


@router.get("/api/research/benter/ledger-backtest")
def benter_ledger_backtest():
    """Run the two-stage walk-forward pilot against settled Points ledger rows."""
    try:
        db_rows = _ledger_rows()
        dataset, metadata, skipped = _make_dataset(db_rows)
        if not dataset:
            return {
                "status": "insufficient_data",
                "database_rows": len(db_rows),
                "eligible_rows": 0,
                "skipped": skipped,
            }

        result = walk_forward_benter(
            dataset,
            minimum_stage1_rows=30,
            minimum_stage2_rows=30,
            stage1_l2=2.0,
            stage2_l2=0.25,
        )
        predictions = []
        for item in result.get("predictions", []):
            meta = metadata[item["input_index"]]
            combined = float(item["combined_probability"])
            market = float(item["market_probability"])
            predictions.append({
                **meta,
                "fundamental_probability": round(float(item["fundamental_probability"]), 5),
                "combined_probability": round(combined, 5),
                "benter_ev_units": round(expected_value_units(combined, 0.0, meta["odds"]), 5),
                "market_ev_units": round(expected_value_units(market, 0.0, meta["odds"]), 5),
                "stage1_training_rows": item["stage1_training_rows"],
                "stage2_training_oos_rows": item["stage2_training_oos_rows"],
            })

        metrics_rows = [
            {**p, "target": 1 if p["outcome"] == "WIN" else 0}
            for p in predictions
        ]
        metrics_rows = [
            {**p, "combined_probability": p["combined_probability"]}
            for p in metrics_rows
        ]
        benter_brier = _brier_for(metrics_rows, "combined_probability")
        market_brier = _brier_for(metrics_rows, "market_probability")
        old_brier = _brier_for(metrics_rows, "old_model_probability")
        ev_picks = [p for p in predictions if p["benter_ev_units"] > 0]
        wins = sum(p["outcome"] == "WIN" for p in ev_picks)
        roi_units = sum(p["odds"] / 100.0 if p["odds"] > 0 else 100.0 / abs(p["odds"])
                        for p in ev_picks if p["outcome"] == "WIN") - sum(
                            1.0 for p in ev_picks if p["outcome"] == "LOSS"
                        )

        return {
            "status": result["status"],
            "model_version": result.get("model_version"),
            "market": "NBA player points",
            "data_source": "Existing settled nba_predictions ledger",
            "market_input": "Stored market_probability from the historical ledger; existing snapshots commonly blend DraftKings and FanDuel and are not a DK-only market archive.",
            "price_basis": "Stored best-side odds; may be DraftKings or FanDuel. Not a DraftKings-only ROI test.",
            "database_rows": len(db_rows),
            "eligible_rows": len(dataset),
            "skipped_rows": len(skipped),
            "feature_order": list(FEATURES),
            "feature_definition": "All player-vs-line features are signed toward the recorded side. No stored model probability is used in stage 1.",
            "dates": {"first": result.get("first_date"), "last": result.get("last_date")},
            "walk_forward": result.get("method"),
            "holdout_count": len(predictions),
            "holdout_brier": {"benter_combined": benter_brier, "market": market_brier,
                              "legacy_model": old_brier},
            "positive_ev_subset_at_stored_best_price": {
                "bets": len(ev_picks), "wins": wins, "losses": len(ev_picks) - wins,
                "win_rate_pct": round(100.0 * wins / len(ev_picks), 2) if ev_picks else None,
                "profit_units": round(roi_units, 4),
                "roi_pct": round(100.0 * roi_units / len(ev_picks), 2) if ev_picks else None,
            },
            "predictions": predictions,
            "date_diagnostics": result.get("diagnostics"),
            "limitations": [
                "This is a selected-pick ledger, not the complete DraftKings player-prop market; selection bias applies.",
                "The current ledger has only a few distinct slate dates. A tiny holdout cannot establish a betting edge.",
                "Historical feature snapshots lack a complete set of usage, injury, lineup, opponent and pace inputs; this pilot uses only fields present on every eligible record.",
                "The stored market probability and best-side price may blend DraftKings and FanDuel; this is not a DraftKings-only test.",
                "The holdout is evaluated at the originally recorded best-side price; no claims about closing-line value are made.",
                "No production data is modified and no picks are generated."
            ],
            "skipped_examples": skipped[:20],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Benter research backtest unavailable: " + str(exc)[:220])
