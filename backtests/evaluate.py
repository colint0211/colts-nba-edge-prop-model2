#!/usr/bin/env python3
"""Leakage-aware evaluator for archived, out-of-sample NBA predictions.

Input is a CSV of predictions generated using only information available before
the game. This script evaluates predictions; it never fabricates missing odds,
lines, projections, or outcomes.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = {"event_date", "market", "model_probability", "odds_american", "outcome"}
VALID = {"WIN", "LOSS", "PUSH"}

def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def num(row, key):
    try:
        x = float(row.get(key, ""))
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def decimal_odds(american):
    a = float(american)
    if a == 0:
        raise ValueError("American odds cannot be zero")
    return 1 + (a / 100 if a > 0 else 100 / abs(a))

def evaluate(rows):
    accepted, rejected = [], []
    for line_no, row in enumerate(rows, 2):
        missing = REQUIRED - set(row)
        if missing:
            raise ValueError("Missing required columns: " + ", ".join(sorted(missing)))
        try:
            p = num(row, "model_probability")
            if p is None or not 0 < p < 1:
                raise ValueError("model_probability must be between 0 and 1")
            outcome = str(row.get("outcome", "")).upper().strip()
            if outcome not in VALID:
                raise ValueError("outcome must be WIN, LOSS, or PUSH")
            start = parse_dt(row.get("event_start") or row["event_date"])
            generated = parse_dt(row.get("prediction_created_at"))
            features = parse_dt(row.get("features_as_of"))
            if generated and start and generated >= start:
                raise ValueError("prediction_created_at is not pregame")
            if features and start and features >= start:
                raise ValueError("features_as_of is not pregame")
            if generated and features and features > generated:
                raise ValueError("features_as_of is later than prediction creation")
            odds = float(row["odds_american"])
            dec = decimal_odds(odds)
            actual = num(row, "actual_value")
            line = num(row, "line")
            projection = num(row, "projection")
            closing = num(row, "closing_line")
            item = dict(row)
            item.update(_p=p, _outcome=outcome, _decimal=dec, _actual=actual,
                        _line=line, _projection=projection, _closing=closing)
            accepted.append(item)
        except Exception as exc:
            rejected.append({"row": line_no, "reason": str(exc)})
    return accepted, rejected

def summarize(rows):
    n = len(rows)
    settled = [r for r in rows if r["_outcome"] != "PUSH"]
    wins = sum(r["_outcome"] == "WIN" for r in settled)
    units = sum((r["_decimal"] - 1) if r["_outcome"] == "WIN" else -1
                for r in rows if r["_outcome"] != "PUSH")
    brier = sum((r["_p"] - (1 if r["_outcome"] == "WIN" else 0))**2
                for r in settled) / len(settled) if settled else None
    eps = 1e-12
    logloss = (-sum((1 if r["_outcome"] == "WIN" else 0) *
                    math.log(max(eps, min(1-eps, r["_p"]))) +
                    (0 if r["_outcome"] == "WIN" else 1) *
                    math.log(max(eps, min(1-eps, 1-r["_p"])))
                    for r in settled) / len(settled)) if settled else None
    mae_rows = [abs(r["_projection"] - r["_actual"]) for r in rows
                if r["_projection"] is not None and r["_actual"] is not None]
    clv_rows = []
    for r in rows:
        if r["_closing"] is None or r["_line"] is None:
            continue
        side = str(r.get("selection", "")).lower()
        # Positive means the selected side got a more favorable line by close.
        delta = (r["_line"] - r["_closing"]) if "over" in side else (r["_closing"] - r["_line"])
        clv_rows.append(delta)
    bins = []
    for lo in [i/10 for i in range(0, 10)]:
        group = [r for r in settled if lo <= r["_p"] < lo + .1]
        if group:
            bins.append({"range": f"{lo:.1f}-{lo+.1:.1f}", "n": len(group),
                         "mean_probability": round(sum(r["_p"] for r in group)/len(group), 4),
                         "win_rate": round(sum(r["_outcome"] == "WIN" for r in group)/len(group), 4)})
    return {
        "bets_including_pushes": n, "settled_bets": len(settled), "wins": wins,
        "losses": len(settled)-wins, "pushes": n-len(settled),
        "win_rate_ex_push": round(wins/len(settled), 4) if settled else None,
        "net_units_flat_1u": round(units, 4),
        "roi_flat_1u_pct": round(100*units/len(settled), 3) if settled else None,
        "brier_score": round(brier, 6) if brier is not None else None,
        "log_loss": round(logloss, 6) if logloss is not None else None,
        "projection_mae": round(sum(mae_rows)/len(mae_rows), 4) if mae_rows else None,
        "mean_closing_line_value": round(sum(clv_rows)/len(clv_rows), 4) if clv_rows else None,
        "calibration_bins": bins,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("--output", default="backtest_report.json")
    ap.add_argument("--group", default="market", choices=["market", "season", "market,season"])
    args = ap.parse_args()
    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        rows, rejected = evaluate(csv.DictReader(f))
    groups = defaultdict(list)
    for r in rows:
        season = r.get("season") or "unspecified"
        market = r.get("market") or "unspecified"
        key = market if args.group == "market" else season if args.group == "season" else market + " | " + season
        groups[key].append(r)
    report = {
        "input_file": str(Path(args.csv_file)),
        "accepted_rows": len(rows),
        "rejected_rows": len(rejected),
        "rejections": rejected,
        "overall": summarize(rows),
        "groups": {k: summarize(v) for k, v in sorted(groups.items())},
        "methodology": "Flat 1-unit stakes; pushes excluded from ROI denominator and probability scores; no missing odds or results imputed. Rows with supplied timestamps are rejected if prediction/features are not pregame. Input probabilities must be generated walk-forward using no future data."
    }
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"accepted_rows": len(rows), "rejected_rows": len(rejected),
                      "overall": report["overall"], "output": args.output}, indent=2))

if __name__ == "__main__":
    main()
