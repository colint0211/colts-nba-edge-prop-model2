"""Durable prediction ledger and out-of-sample model evaluation for Colt's NBA Edge."""
import os
import json
import hashlib
import math
from datetime import datetime, timezone

def _connect():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured. Connect the Neon database in Vercel.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)

def initialize():
    with _connect() as conn:
        conn.execute("""
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
        CREATE INDEX IF NOT EXISTS nba_predictions_market_created ON nba_predictions(market, created_at DESC);
        CREATE INDEX IF NOT EXISTS nba_predictions_unsettled ON nba_predictions(settled_at) WHERE settled_at IS NULL;
        """)

def _number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def save_predictions(rows, market=None):
    if not isinstance(rows, list):
        return {"saved": 0, "skipped": 0}
    saved = skipped = 0
    with _connect() as conn:
        for p in rows:
            if not isinstance(p, dict):
                skipped += 1
                continue
            m = str(p.get("market") or market or "points").lower()
            player = str(p.get("player") or "").strip()
            line = _number(p.get("line"))
            side = str(p.get("best_side") or "").upper()
            prob = _number(p.get("best_probability"))
            odds = p.get("best_odds")
            try:
                odds = int(odds) if odds is not None else None
            except (TypeError, ValueError):
                odds = None
            if not player or line is None or side not in ("OVER", "UNDER") or prob is None or not 0 < prob < 100 or odds is None or odds == 0:
                skipped += 1
                continue
            model_prob = _number(p.get("model_over_probability" if side == "OVER" else "model_under_probability"))
            market_prob = _number(p.get("market_over_probability" if side == "OVER" else "market_under_probability"))
            model_prob = model_prob if model_prob is not None else prob
            event = str(p.get("game") or "")
            event_id = str(p.get("event_id") or p.get("game_id") or "")
            # Stable fingerprint deduplicates refreshes; a changed price/probability is retained as a new snapshot.
            identity = "|".join([m, player.lower(), event.lower(), str(line), side, str(odds), f"{prob:.3f}"])
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            snapshot = json.dumps(p, default=str, separators=(",", ":"))
            cur = conn.execute("""
              INSERT INTO nba_predictions
              (fingerprint,market,player,team,event,event_id,line,side,odds,model_probability,market_probability,blended_probability,projection,snapshot)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
              ON CONFLICT (fingerprint) DO NOTHING
            """, (fingerprint,m,player,p.get("team"),event,event_id,line,side,odds,
                  model_prob/100, market_prob/100 if market_prob is not None else None,
                  prob/100,_number(p.get("projection")),snapshot))
            if cur.rowcount:
                saved += 1
            else:
                skipped += 1
    return {"saved": saved, "skipped": skipped}


def list_custom_lines(limit=500):
    """Return the latest saved user-entered line/odds snapshot per player, market and event."""
    with _connect() as conn:
        rows = conn.execute("""
          SELECT snapshot FROM nba_predictions
          WHERE snapshot->>'custom_line' = 'true'
          ORDER BY created_at DESC
          LIMIT %s
        """, (max(1, min(int(limit), 1000)),)).fetchall()
    latest = {}
    for row in rows:
        snapshot = row[0]
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except Exception:
                continue
        if not isinstance(snapshot, dict):
            continue
        market = str(snapshot.get("market") or "").lower()
        player = str(snapshot.get("player") or "").strip().lower()
        event = str(snapshot.get("game") or "").strip().lower()
        if not market or not player:
            continue
        key = (market, player, event)
        if key not in latest:
            latest[key] = snapshot
    return list(latest.values())

def settle_prediction(prediction_id, actual_value):
    actual = _number(actual_value)
    if actual is None:
        raise ValueError("actual_value must be numeric")
    with _connect() as conn:
        row = conn.execute("SELECT id,line,side,odds FROM nba_predictions WHERE id=%s FOR UPDATE", (prediction_id,)).fetchone()
        if not row:
            raise LookupError("Prediction not found")
        if row[0] is None:
            raise LookupError("Prediction not found")
        _, line, side, odds = row
        if actual == line:
            outcome, profit = "PUSH", 0.0
        else:
            won = actual > line if side == "OVER" else actual < line
            outcome = "WIN" if won else "LOSS"
            profit = (odds / 100 if odds > 0 else 100 / abs(odds)) if won else -1.0
        conn.execute("""UPDATE nba_predictions SET actual_value=%s,outcome=%s,profit_units=%s,settled_at=NOW()
                        WHERE id=%s AND settled_at IS NULL""", (actual,outcome,profit,prediction_id))
        return {"id": prediction_id, "outcome": outcome, "profit_units": profit}

def metrics(market=None):
    where = "WHERE settled_at IS NOT NULL"
    args = []
    if market and market != "all":
        where += " AND market=%s"
        args.append(market)
    with _connect() as conn:
        rows = conn.execute(f"""SELECT id,market,model_probability,blended_probability,outcome,profit_units
                               FROM nba_predictions {where} ORDER BY settled_at DESC""", args).fetchall()
        counts = conn.execute(f"""SELECT COUNT(*),COUNT(*) FILTER (WHERE outcome='WIN'),
          COUNT(*) FILTER (WHERE outcome='LOSS'),COUNT(*) FILTER (WHERE outcome='PUSH'),
          COALESCE(SUM(profit_units),0) FROM nba_predictions {where}""", args).fetchone()
        total,wins,losses,pushes,profit = counts
        decisions = [r for r in rows if r[4] in ("WIN","LOSS")]
        n = len(decisions)
        brier = sum((r[3] - (1 if r[4]=="WIN" else 0))**2 for r in decisions)/n if n else None
        eps=1e-12
        logloss = -sum((math.log(max(eps,r[3])) if r[4]=="WIN" else math.log(max(eps,1-r[3]))) for r in decisions)/n if n else None
        bins=[]
        for lo in [0,.5,.55,.6,.65,.7,.75,.8,.85,.9]:
            hi = lo+.05 if lo else .5
            group=[r for r in decisions if (lo <= r[3] < hi if lo else r[3] < .5)]
            if group:
                bins.append({"range": f"{lo*100:.0f}-{hi*100:.0f}%", "count":len(group),
                  "predicted":round(sum(r[3] for r in group)/len(group)*100,2),
                  "observed":round(sum(1 if r[4]=="WIN" else 0 for r in group)/len(group)*100,2)})
        by_market={}
        for m in sorted(set(r[1] for r in rows)):
            subset=[r for r in rows if r[1]==m]
            w=sum(r[4]=="WIN" for r in subset); l=sum(r[4]=="LOSS" for r in subset)
            by_market[m]={"settled":len(subset),"wins":w,"losses":l,
              "win_rate":round(w/(w+l)*100,2) if w+l else None,
              "profit_units":round(sum(r[5] or 0 for r in subset),3)}
        return {"settled":total,"wins":wins,"losses":losses,"pushes":pushes,
          "win_rate":round(wins/(wins+losses)*100,2) if wins+losses else None,
          "profit_units":round(float(profit),3),"roi_pct":round(float(profit)/(wins+losses)*100,2) if wins+losses else None,
          "brier_score":round(brier,5) if brier is not None else None,
          "log_loss":round(logloss,5) if logloss is not None else None,
          "calibration_bins":bins,"by_market":by_market}

def list_predictions(status="unsettled", market=None, limit=100):
    clauses=[]; args=[]
    if status=="unsettled": clauses.append("settled_at IS NULL")
    elif status=="settled": clauses.append("settled_at IS NOT NULL")
    if market and market!="all": clauses.append("market=%s"); args.append(market)
    where=("WHERE "+" AND ".join(clauses)) if clauses else ""
    args.append(max(1,min(int(limit),500)))
    with _connect() as conn:
        rows=conn.execute(f"""SELECT id,market,player,team,event,line,side,odds,blended_probability,projection,created_at
          FROM nba_predictions {where} ORDER BY created_at DESC LIMIT %s""",args).fetchall()
        return [{"id":r[0],"market":r[1],"player":r[2],"team":r[3],"game":r[4],"line":r[5],
          "side":r[6],"odds":r[7],"probability_pct":round(r[8]*100,2),"projection":r[9],
          "created_at":r[10].isoformat() if r[10] else None} for r in rows]
