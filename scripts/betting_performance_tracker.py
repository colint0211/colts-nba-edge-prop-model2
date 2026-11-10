#!/usr/bin/env python3
"""Append-only, auditable betting performance tracker for NBA props and ATS.

Input CSV columns:
play_id,model,qualified_at,sportsbook,market,selection,line,odds_american,
model_probability,result,net_units,source_record_id,qualification_rule_version,
verification_status

Required qualification fields are preserved even when missing; incomplete rows
are ingested and flagged rather than silently discarded. Use result=PENDING
until settled. For settled rows, net_units may be blank and will be calculated
from a 1-unit risk and American odds.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "event_id", "event_type", "play_id", "model", "qualified_at", "sportsbook",
    "market", "selection", "line", "odds_american", "model_probability",
    "result", "net_units", "risk_units", "source_record_id",
    "qualification_rule_version", "verification_status", "audit_flags",
]
QUAL_REQUIRED = [
    "play_id", "model", "qualified_at", "sportsbook", "line",
    "odds_american", "model_probability",
]
VALID_RESULTS = {"PENDING", "WIN", "LOSS", "PUSH"}


def norm_result(value: str) -> str:
    value = (value or "PENDING").strip().upper()
    aliases = {"W": "WIN", "L": "LOSS", "P": "PUSH", "OPEN": "PENDING"}
    return aliases.get(value, value)


def american_profit(odds: float, result: str, risk: float = 1.0) -> float:
    if result == "LOSS":
        return -risk
    if result == "PUSH":
        return 0.0
    if result != "WIN":
        return 0.0
    if odds > 0:
        return risk * odds / 100.0
    if odds < 0:
        return risk * 100.0 / abs(odds)
    raise ValueError("American odds cannot be zero")


def flags_for(row: dict) -> list[str]:
    flags = []
    for field in QUAL_REQUIRED:
        if not str(row.get(field, "")).strip():
            flags.append("MISSING_" + field.upper())
    try:
        p = float(row.get("model_probability", ""))
        if not 0 <= p <= 1:
            flags.append("INVALID_MODEL_PROBABILITY")
    except (TypeError, ValueError):
        if "MISSING_MODEL_PROBABILITY" not in flags:
            flags.append("INVALID_MODEL_PROBABILITY")
    try:
        odds = float(row.get("odds_american", ""))
        if odds == 0:
            flags.append("INVALID_AMERICAN_ODDS")
    except (TypeError, ValueError):
        if "MISSING_ODDS_AMERICAN" not in flags:
            flags.append("INVALID_AMERICAN_ODDS")
    try:
        datetime.fromisoformat(str(row.get("qualified_at", "")).replace("Z", "+00:00"))
    except ValueError:
        if row.get("qualified_at"):
            flags.append("UNVERIFIABLE_TIMESTAMP")
    if not row.get("source_record_id"):
        flags.append("MISSING_SOURCE_RECORD_ID")
    if not row.get("qualification_rule_version"):
        flags.append("MISSING_QUALIFICATION_RULE_VERSION")
    if row.get("verification_status", "").upper() not in {"VERIFIED", "UNVERIFIED"}:
        flags.append("UNVERIFIED_SOURCE_STATUS")
    return sorted(set(flags))


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_events(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists or path.stat().st_size == 0:
            writer.writeheader()
        writer.writerows(rows)


def ingest(source: Path, ledger: Path, model_override: str | None = None) -> dict:
    existing = read_events(ledger)
    qualified_ids = {r["play_id"] for r in existing if r["event_type"] == "QUALIFIED"}
    events = []
    duplicates = 0
    with source.open(newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            row = {k: (raw.get(k) or "").strip() for k in raw}
            pid = row.get("play_id", "")
            if not pid:
                # Keep the row auditable, but do not invent a stable identity.
                pid = "UNIDENTIFIED-" + str(sum(1 for x in events) + 1)
                row["play_id"] = pid
            if pid in qualified_ids:
                duplicates += 1
                continue
            if model_override:
                row["model"] = model_override
            result = norm_result(row.get("result", "PENDING"))
            if result not in VALID_RESULTS:
                result = "PENDING"
                row["verification_status"] = "UNVERIFIED"
            try:
                net = float(row["net_units"]) if row.get("net_units") else (
                    american_profit(float(row["odds_american"]), result)
                    if result in {"WIN", "LOSS", "PUSH"} else 0.0
                )
            except (ValueError, TypeError):
                net = 0.0
                row["verification_status"] = "UNVERIFIED"
            flags = flags_for(row)
            if pid.startswith("UNIDENTIFIED-"):
                flags.append("MISSING_STABLE_PLAY_ID")
            if result in {"WIN", "LOSS", "PUSH"} and not row.get("net_units"):
                flags.append("NET_UNITS_DERIVED_FROM_ODDS")
            event = {
                "event_id": f"{pid}:QUALIFIED",
                "event_type": "QUALIFIED",
                "play_id": pid,
                "model": row.get("model", ""),
                "qualified_at": row.get("qualified_at", ""),
                "sportsbook": row.get("sportsbook", ""),
                "market": row.get("market", ""),
                "selection": row.get("selection", ""),
                "line": row.get("line", ""),
                "odds_american": row.get("odds_american", ""),
                "model_probability": row.get("model_probability", ""),
                "result": result,
                "net_units": f"{net:.6f}",
                "risk_units": "1.0",
                "source_record_id": row.get("source_record_id", ""),
                "qualification_rule_version": row.get("qualification_rule_version", ""),
                "verification_status": row.get("verification_status", "UNVERIFIED") or "UNVERIFIED",
                "audit_flags": "|".join(sorted(set(flags))),
            }
            events.append(event)
            qualified_ids.add(pid)
    write_events(ledger, events)
    return {"ingested": len(events), "duplicates_skipped": duplicates,
            "flagged": sum(bool(e["audit_flags"]) for e in events)}


def settle(ledger: Path, play_id: str, result: str, net_units: float | None) -> None:
    events = read_events(ledger)
    qualified = next((r for r in events if r["event_type"] == "QUALIFIED" and r["play_id"] == play_id), None)
    if not qualified:
        raise SystemExit(f"No qualifying record found for play_id={play_id}")
    if any(r["event_type"] == "SETTLED" and r["play_id"] == play_id for r in events):
        raise SystemExit("Play already settled. Existing settlement is immutable.")
    result = norm_result(result)
    if result not in {"WIN", "LOSS", "PUSH"}:
        raise SystemExit("Settlement must be WIN, LOSS, or PUSH.")
    if net_units is None:
        odds = float(qualified["odds_american"])
        net_units = american_profit(odds, result, 1.0)
    event = dict(qualified)
    event.update({
        "event_id": f"{play_id}:SETTLED",
        "event_type": "SETTLED",
        "result": result,
        "net_units": f"{net_units:.6f}",
        "audit_flags": qualified.get("audit_flags", ""),
    })
    write_events(ledger, [event])


def resolved_plays(ledger: Path) -> list[dict]:
    events = read_events(ledger)
    qualified = {r["play_id"]: r for r in events if r["event_type"] == "QUALIFIED"}
    settled = {r["play_id"]: r for r in events if r["event_type"] == "SETTLED"}
    out = []
    for pid, q in qualified.items():
        s = settled.get(pid)
        item = dict(q)
        if s:
            item.update({"result": s["result"], "net_units": s["net_units"], "settled": True})
        else:
            item.update({"result": "PENDING", "net_units": "0", "settled": False})
        out.append(item)
    return sorted(out, key=lambda x: (x.get("qualified_at", ""), x["play_id"]))


def summarize(ledger: Path) -> dict:
    plays = resolved_plays(ledger)
    settled = [p for p in plays if p["result"] in {"WIN", "LOSS", "PUSH"}]
    wins = sum(p["result"] == "WIN" for p in settled)
    losses = sum(p["result"] == "LOSS" for p in settled)
    pushes = sum(p["result"] == "PUSH" for p in settled)
    units = [float(p["net_units"] or 0) for p in settled]
    cumulative, peak, max_dd = 0.0, 0.0, 0.0
    curve = []
    for u in units:
        cumulative += u
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
        curve.append(round(cumulative, 4))
    risk = sum(float(p.get("risk_units") or 1) for p in settled)
    net = sum(units)
    flagged = sum(bool(p.get("audit_flags")) for p in plays)
    return {
        "qualifying_plays": len(plays), "settled_plays": len(settled),
        "pending_plays": len(plays) - len(settled),
        "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate_ex_pushes": wins / (wins + losses) if wins + losses else None,
        "net_units": round(net, 4), "risked_units": round(risk, 4),
        "roi": net / risk if risk else None,
        "maximum_drawdown_units": round(max_dd, 4),
        "cumulative_profit_curve": curve, "flagged_records": flagged,
        "unverified_or_incomplete_records": flagged,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="Append qualifying plays from a CSV export")
    i.add_argument("csv", type=Path)
    i.add_argument("--ledger", type=Path, default=Path("data/performance_events.csv"))
    i.add_argument("--model", help="Optional model label override")
    s = sub.add_parser("settle", help="Append an immutable settlement event")
    s.add_argument("play_id")
    s.add_argument("result", choices=["WIN", "LOSS", "PUSH"])
    s.add_argument("--net-units", type=float)
    s.add_argument("--ledger", type=Path, default=Path("data/performance_events.csv"))
    r = sub.add_parser("report", help="Print overall and per-model performance")
    r.add_argument("--ledger", type=Path, default=Path("data/performance_events.csv"))
    args = ap.parse_args()
    if args.cmd == "ingest":
        print(json.dumps(ingest(args.csv, args.ledger, args.model), indent=2))
    elif args.cmd == "settle":
        settle(args.ledger, args.play_id, args.result, args.net_units)
        print("Settlement event appended.")
    else:
        plays = resolved_plays(args.ledger)
        report = {"overall": summarize(args.ledger), "by_model": {}}
        for model in sorted({p.get("model", "") for p in plays}):
            subset = [p for p in plays if p.get("model", "") == model]
            # Summarize model slice using the same event semantics.
            tmp = args.ledger.with_name(args.ledger.stem + "_model_tmp.csv")
            # Avoid writing temporary files: calculate directly.
            settled = [p for p in subset if p["result"] in {"WIN", "LOSS", "PUSH"}]
            w = sum(p["result"] == "WIN" for p in settled)
            l = sum(p["result"] == "LOSS" for p in settled)
            u = sum(float(p["net_units"] or 0) for p in settled)
            risk = len(settled)
            curve = 0.0
            peak = 0.0
            dd = 0.0
            for p in sorted(settled, key=lambda x: (x.get("qualified_at", ""), x["play_id"])):
                curve += float(p["net_units"] or 0)
                peak = max(peak, curve)
                dd = max(dd, peak - curve)
            report["by_model"][model] = {
                "qualifying_plays": len(subset), "settled_plays": len(settled),
                "pending_plays": len(subset)-len(settled), "wins": w, "losses": l,
                "pushes": sum(p["result"] == "PUSH" for p in settled),
                "win_rate_ex_pushes": w/(w+l) if w+l else None,
                "net_units": round(u, 4), "risked_units": risk,
                "roi": u/risk if risk else None, "maximum_drawdown_units": round(dd, 4),
                "flagged_records": sum(bool(p.get("audit_flags")) for p in subset),
            }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
