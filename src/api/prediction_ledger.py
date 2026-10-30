"""Durable prediction ledger and out-of-sample model evaluation for Colt's NBA Edge."""
import hashlib
import json
import math
import os


def _connect():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not configured. Connect the Neon database in Vercel."
        )
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def initialize():
    """Create the ledger and its indexes without altering existing user data."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nba_predictions (
              id BIGSERIAL PRIMARY KEY,
              fingerprint TEXT NOT NULL UNIQUE,
              market TEXT NOT NULL,
              player TEXT NOT NULL,
              team TEXT,
              event TEXT,
              event_id TEXT,
              line DOUBLE PRECISION NOT NULL,
              side TEXT NOT NULL,
              odds INTEGER,
              model_probability DOUBLE PRECISION NOT NULL,
              market_probability DOUBLE PRECISION,
              blended_probability DOUBLE PRECISION NOT NULL,
              projection DOUBLE PRECISION,
              model_version TEXT NOT NULL DEFAULT 'benter-inspired-v1',
              snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              settled_at TIMESTAMPTZ,
              actual_value DOUBLE PRECISION,
              outcome TEXT,
              profit_units DOUBLE PRECISION
            );
            CREATE INDEX IF NOT EXISTS nba_predictions_market_created
              ON nba_predictions(market, created_at DESC);
            CREATE INDEX IF NOT EXISTS nba_predictions_unsettled
              ON nba_predictions(settled_at) WHERE settled_at IS NULL;
            CREATE INDEX IF NOT EXISTS nba_predictions_custom_lines
              ON nba_predictions(created_at DESC)
              WHERE snapshot->>'custom_line' = 'true';
            """
        )


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _american_odds(value):
    try:
        odds = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if odds == 0 or abs(odds) > 10000:
        return None
    if odds < 0 and abs(odds) < 100:
        return None
    return odds


def save_predictions(rows, market=None):
    """
    Save immutable prediction snapshots.

    A refresh with an unchanged fingerprint is ignored. A changed line, price,
    side, or probability creates a new historical snapshot rather than
    overwriting a prior prediction.
    """
    if not isinstance(rows, list):
        return {"saved": 0, "skipped": 0}

    saved = skipped = 0
    with _connect() as conn:
        for prediction in rows:
            if not isinstance(prediction, dict):
                skipped += 1
                continue

            market_name = str(
                prediction.get("market") or market or "points"
            ).strip().lower()
            player = str(prediction.get("player") or "").strip()
            line = _number(prediction.get("line"))
            side = str(prediction.get("best_side") or "").strip().upper()
            probability = _number(prediction.get("best_probability"))
            odds = _american_odds(prediction.get("best_odds"))

            if (
                not market_name
                or not player
                or line is None
                or side not in ("OVER", "UNDER")
                or probability is None
                or not 0 < probability < 100
                or odds is None
            ):
                skipped += 1
                continue

            model_probability = _number(
                prediction.get(
                    "model_over_probability"
                    if side == "OVER"
                    else "model_under_probability"
                )
            )
            market_probability = _number(
                prediction.get(
                    "market_over_probability"
                    if side == "OVER"
                    else "market_under_probability"
                )
            )
            if model_probability is None:
                model_probability = probability
            if not 0 <= model_probability <= 100:
                skipped += 1
                continue
            if market_probability is not None and not 0 <= market_probability <= 100:
                market_probability = None

            event = str(prediction.get("game") or "")
            event_id = str(
                prediction.get("event_id") or prediction.get("game_id") or ""
            )
            identity = "|".join(
                (
                    market_name,
                    player.casefold(),
                    event_id.casefold() or event.casefold(),
                    format(line, ".4f"),
                    side,
                    str(odds),
                    format(probability, ".3f"),
                )
            )
            fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            snapshot = json.dumps(
                prediction, default=str, separators=(",", ":"), allow_nan=False
            )

            cursor = conn.execute(
                """
                INSERT INTO nba_predictions
                  (fingerprint, market, player, team, event, event_id, line,
                   side, odds, model_probability, market_probability,
                   blended_probability, projection, snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (fingerprint) DO NOTHING
                """,
                (
                    fingerprint,
                    market_name,
                    player,
                    prediction.get("team"),
                    event,
                    event_id,
                    line,
                    side,
                    odds,
                    model_probability / 100.0,
                    market_probability / 100.0
                    if market_probability is not None
                    else None,
                    probability / 100.0,
                    _number(prediction.get("projection")),
                    snapshot,
                ),
            )
            if cursor.rowcount:
                saved += 1
            else:
                skipped += 1

    return {"saved": saved, "skipped": skipped}


def list_custom_lines(limit=500):
    """
    Return the newest user-entered line/odds snapshot per player, market,
    and event. The complete snapshot is returned to preserve frontend fields.
    """
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        requested_limit = 500
    requested_limit = max(1, min(requested_limit, 5000))

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT snapshot
            FROM nba_predictions
            WHERE snapshot->>'custom_line' = 'true'
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (requested_limit,),
        ).fetchall()

    latest = {}
    for row in rows:
        snapshot = row[0]
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (TypeError, ValueError):
                continue
        if not isinstance(snapshot, dict):
            continue

        market_name = str(snapshot.get("market") or "").strip().lower()
        player = str(snapshot.get("player") or "").strip().casefold()
        event_id = str(snapshot.get("event_id") or snapshot.get("game_id") or "").strip()
        event = str(snapshot.get("game") or "").strip().casefold()
        event_key = event_id.casefold() or event
        if not market_name or not player:
            continue
        key = (market_name, player, event_key)
        if key not in latest:
            latest[key] = snapshot

    return list(latest.values())


def settle_prediction(prediction_id, actual_value):
    """Settle one prediction exactly once; a settled result cannot be overwritten."""
    try:
        prediction_id = int(prediction_id)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("prediction_id must be an integer")

    actual = _number(actual_value)
    if actual is None:
        raise ValueError("actual_value must be numeric")
    if actual < 0:
        raise ValueError("actual_value cannot be negative")

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, line, side, odds, settled_at
            FROM nba_predictions
            WHERE id = %s
            FOR UPDATE
            """,
            (prediction_id,),
        ).fetchone()
        if not row:
            raise LookupError("Prediction not found")
        _, line, side, odds, settled_at = row
        if settled_at is not None:
            raise ValueError("This prediction has already been settled")
        if odds is None or odds == 0:
            raise RuntimeError("Prediction has invalid stored odds")

        if math.isclose(actual, float(line), rel_tol=0.0, abs_tol=1e-9):
            outcome, profit = "PUSH", 0.0
        else:
            won = actual > line if side == "OVER" else actual < line
            outcome = "WIN" if won else "LOSS"
            profit = (
                odds / 100.0 if odds > 0 else 100.0 / abs(odds)
            ) if won else -1.0

        cursor = conn.execute(
            """
            UPDATE nba_predictions
            SET actual_value=%s, outcome=%s, profit_units=%s, settled_at=NOW()
            WHERE id=%s AND settled_at IS NULL
            """,
            (actual, outcome, profit, prediction_id),
        )
        if not cursor.rowcount:
            raise ValueError("This prediction has already been settled")

    return {
        "id": prediction_id,
        "outcome": outcome,
        "profit_units": round(profit, 4),
    }


def _probability_metrics(rows, probability_index=3):
    decisions = [
        row for row in rows if row[4] in ("WIN", "LOSS")
        and _number(row[probability_index]) is not None
    ]
    count = len(decisions)
    if not count:
        return {"brier_score": None, "log_loss": None, "count": 0}

    epsilon = 1e-12
    brier = 0.0
    logloss = 0.0
    for row in decisions:
        probability = max(epsilon, min(1.0 - epsilon, float(row[probability_index])))
        actual = 1.0 if row[4] == "WIN" else 0.0
        brier += (probability - actual) ** 2
        logloss -= math.log(probability if actual else 1.0 - probability)

    return {
        "brier_score": round(brier / count, 5),
        "log_loss": round(logloss / count, 5),
        "count": count,
    }


def metrics(market=None):
    where = "WHERE settled_at IS NOT NULL"
    args = []
    if market and market != "all":
        where += " AND market=%s"
        args.append(market)

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, market, model_probability, blended_probability,
                   outcome, profit_units
            FROM nba_predictions
            {where}
            ORDER BY settled_at DESC
            """,
            args,
        ).fetchall()
        counts = conn.execute(
            f"""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE outcome='WIN'),
                   COUNT(*) FILTER (WHERE outcome='LOSS'),
                   COUNT(*) FILTER (WHERE outcome='PUSH'),
                   COALESCE(SUM(profit_units), 0)
            FROM nba_predictions
            {where}
            """,
            args,
        ).fetchone()

    total, wins, losses, pushes, profit = counts
    decisions = [row for row in rows if row[4] in ("WIN", "LOSS")]
    blended_metrics = _probability_metrics(rows, 3)
    model_metrics = _probability_metrics(rows, 2)

    bins = []
    ranges = [(0.0, 0.50)] + [
        (value / 100.0, (value + 5) / 100.0)
        for value in range(50, 100, 5)
    ]
    for lower, upper in ranges:
        group = [row for row in decisions if lower <= row[3] < upper]
        if group:
            bins.append(
                {
                    "range": f"{lower * 100:.0f}-{upper * 100:.0f}%",
                    "count": len(group),
                    "predicted": round(
                        sum(row[3] for row in group) / len(group) * 100, 2
                    ),
                    "observed": round(
                        sum(1 if row[4] == "WIN" else 0 for row in group)
                        / len(group) * 100,
                        2,
                    ),
                }
            )

    by_market = {}
    for market_name in sorted({row[1] for row in rows}):
        subset = [row for row in rows if row[1] == market_name]
        subset_decisions = [row for row in subset if row[4] in ("WIN", "LOSS")]
        subset_wins = sum(row[4] == "WIN" for row in subset_decisions)
        subset_losses = sum(row[4] == "LOSS" for row in subset_decisions)
        subset_profit = sum(row[5] or 0.0 for row in subset)
        by_market[market_name] = {
            "settled": len(subset),
            "wins": subset_wins,
            "losses": subset_losses,
            "pushes": sum(row[4] == "PUSH" for row in subset),
            "win_rate": round(
                subset_wins / (subset_wins + subset_losses) * 100, 2
            ) if subset_wins + subset_losses else None,
            "profit_units": round(subset_profit, 3),
        }

    decision_total = wins + losses
    total_profit = float(profit or 0.0)
    return {
        "settled": total,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": round(wins / decision_total * 100, 2)
        if decision_total else None,
        "profit_units": round(total_profit, 3),
        "roi_pct": round(total_profit / decision_total * 100, 2)
        if decision_total else None,
        "brier_score": blended_metrics["brier_score"],
        "log_loss": blended_metrics["log_loss"],
        "model_brier_score": model_metrics["brier_score"],
        "model_log_loss": model_metrics["log_loss"],
        "calibration_sample_size": blended_metrics["count"],
        "model_calibration_sample_size": model_metrics["count"],
        "calibration_bins": bins,
        "by_market": by_market,
    }



def list_settlement_candidates(limit=500):
    """Return unsettled snapshots with their saved identity and creation date."""
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        requested_limit = 500
    requested_limit = max(1, min(requested_limit, 2000))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, market, player, created_at, snapshot
            FROM nba_predictions
            WHERE settled_at IS NULL
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (requested_limit,),
        ).fetchall()
    candidates = []
    for row in rows:
        snapshot = row[4]
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (TypeError, ValueError):
                snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        candidates.append({
            "id": row[0],
            "market": row[1],
            "player": row[2],
            "created_date": row[3].date().isoformat() if row[3] else None,
            "snapshot": snapshot,
        })
    return candidates


def list_predictions(status="unsettled", market=None, limit=100):
    clauses = []
    args = []
    if status == "unsettled":
        clauses.append("settled_at IS NULL")
    elif status == "settled":
        clauses.append("settled_at IS NOT NULL")
    if market and market != "all":
        clauses.append("market=%s")
        args.append(market)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        requested_limit = 100
    args.append(max(1, min(requested_limit, 500)))

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, market, player, team, event, line, side, odds,
                   blended_probability, projection, created_at,
                   outcome, actual_value, profit_units
            FROM nba_predictions
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            args,
        ).fetchall()

    return [
        {
            "id": row[0],
            "market": row[1],
            "player": row[2],
            "team": row[3],
            "game": row[4],
            "line": row[5],
            "side": row[6],
            "odds": row[7],
            "probability_pct": round(row[8] * 100, 2),
            "projection": row[9],
            "created_at": row[10].isoformat() if row[10] else None,
            "outcome": row[11],
            "actual_value": row[12],
            "profit_units": row[13],
        }
        for row in rows
    ]
