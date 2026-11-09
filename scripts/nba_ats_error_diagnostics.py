"""Diagnose ATS model misspecification without changing the betting formula.

Uses game-level predictions from the verified-source forensic audit. Reports
forecast residuals by market spread, model edge, calendar period, favourite/
underdog status, and rest. Also checks whether model errors are systematically
associated with projected team strength or total. This is diagnostic, not
threshold optimization and not a new pick model.
"""
from __future__ import annotations
import csv, json, math
from collections import defaultdict
from pathlib import Path

SOURCE = Path("backtest-output/game_by_game_forensic_audit_v2/every_game.csv")
OUT = Path("backtest-output/ats_error_diagnostics.json")


def num(row, key):
    try:
        x = float(row.get(key, ""))
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def bucket(x, cuts, labels):
    if x is None:
        return "missing"
    for cut, label in zip(cuts, labels):
        if x < cut:
            return label
    return labels[-1] if labels else "all"


def summarize(rows):
    if not rows:
        return {"games": 0}
    margin_err = [r["margin_error"] for r in rows]
    ats_err = [r["ats_error"] for r in rows]
    total_err = [r["total_error"] for r in rows]
    return {
        "games": len(rows),
        "mean_projected_margin": round(avg([r["projected_margin"] for r in rows]), 3),
        "mean_actual_margin": round(avg([r["actual_margin"] for r in rows]), 3),
        "mean_margin_error_model_minus_actual": round(avg(margin_err), 3),
        "margin_mae": round(avg([abs(x) for x in margin_err]), 3),
        "mean_closing_spread_home": round(avg([r["spread"] for r in rows]), 3),
        "mean_model_ats_edge_home": round(avg([r["edge"] for r in rows]), 3),
        "mean_actual_ats_margin_home": round(avg(ats_err), 3),
        "ats_margin_mae": round(avg([abs(x) for x in ats_err]), 3),
        "mean_projected_total": round(avg([r["projected_total"] for r in rows]), 3),
        "mean_actual_total": round(avg([r["actual_total"] for r in rows]), 3),
        "mean_total_error_model_minus_actual": round(avg(total_err), 3),
        "total_mae": round(avg([abs(x) for x in total_err]), 3),
    }


def main():
    if not SOURCE.exists():
        raise RuntimeError(f"Required game audit not found: {SOURCE}")
    with SOURCE.open(newline="") as f:
        raw = list(csv.DictReader(f))
    rows = []
    for r in raw:
        pm, am = num(r, "projected_margin"), num(r, "actual_home_margin")
        spread = num(r, "home_spread")
        pt, at = num(r, "projected_total"), num(r, "actual_total")
        total_line = num(r, "closing_total")
        if None in (pm, am, spread, pt, at):
            continue
        edge = pm + spread
        rows.append({
            "season": r.get("season", ""),
            "date": r.get("date", ""),
            "projected_margin": pm,
            "actual_margin": am,
            "margin_error": pm - am,
            "spread": spread,
            "edge": edge,
            "ats_error": am + spread,
            "projected_total": pt,
            "actual_total": at,
            "total_error": pt - at,
            "closing_total": total_line,
            "home_rest": num(r, "home_rest_days"),
            "away_rest": num(r, "away_rest_days"),
            "home_ortg": num(r, "home_team_ortg"),
            "away_ortg": num(r, "away_team_ortg"),
            "home_drtg": num(r, "home_team_drtg"),
            "away_drtg": num(r, "away_team_drtg"),
            "home_pace": num(r, "home_team_pace"),
            "away_pace": num(r, "away_team_pace"),
        })

    groups = {}
    def add_group(name, fn):
        d = defaultdict(list)
        for r in rows:
            d[fn(r)].append(r)
        groups[name] = {k: summarize(v) for k, v in sorted(d.items())}

    for season in sorted(set(r["season"] for r in rows)):
        groups.setdefault("by_season", {})[season] = summarize([r for r in rows if r["season"] == season])

    add_group("closing_spread_magnitude", lambda r: bucket(abs(r["spread"]), [3, 6, 10, 15], ["0-2.5", "3-5.5", "6-9.5", "10-14.5", "15+"]))
    add_group("model_edge_magnitude", lambda r: bucket(abs(r["edge"]), [1, 2.5, 4, 6, 10], ["0-0.9", "1-2.4", "2.5-3.9", "4-5.9", "6-9.9", "10+"]))
    add_group("model_edge_direction", lambda r: "home_edge" if r["edge"] > 0 else "away_edge" if r["edge"] < 0 else "zero")
    add_group("home_favorite_or_dog", lambda r: "home_favorite" if r["spread"] < 0 else "home_underdog" if r["spread"] > 0 else "pickem")
    add_group("calendar_period", lambda r: r["date"][:7])
    add_group("closing_total", lambda r: bucket(r["closing_total"], [215, 225, 235, 245], ["under215", "215-224.9", "225-234.9", "235-244.9", "245+"]))
    add_group("home_rest_days", lambda r: bucket(r["home_rest"], [1, 2, 3, 5], ["0", "1", "2", "3-4", "5+"]))
    add_group("away_rest_days", lambda r: bucket(r["away_rest"], [1, 2, 3, 5], ["0", "1", "2", "3-4", "5+"]))
    add_group("home_offense_rating", lambda r: bucket(r["home_ortg"], [108, 112, 116, 120], ["<108", "108-111.9", "112-115.9", "116-119.9", "120+"]))
    add_group("away_offense_rating", lambda r: bucket(r["away_ortg"], [108, 112, 116, 120], ["<108", "108-111.9", "112-115.9", "116-119.9", "120+"]))
    add_group("home_defense_rating", lambda r: bucket(r["home_drtg"], [108, 112, 116, 120], ["<108", "108-111.9", "112-115.9", "116-119.9", "120+"]))
    add_group("away_defense_rating", lambda r: bucket(r["away_drtg"], [108, 112, 116, 120], ["<108", "108-111.9", "112-115.9", "116-119.9", "120+"]))

    # Error correlations identify candidate missing variables; they are not
    # evidence of causal effects and must be tested on later untouched games.
    correlations = {}
    for feature in ("spread", "edge", "projected_margin", "projected_total",
                    "closing_total", "home_rest", "away_rest", "home_ortg",
                    "away_ortg", "home_drtg", "away_drtg", "home_pace", "away_pace"):
        pairs = [(r[feature], r["ats_error"]) for r in rows if r[feature] is not None]
        if len(pairs) < 3:
            continue
        xs, ys = zip(*pairs)
        mx, my = avg(xs), avg(ys)
        vx = sum((x-mx)**2 for x in xs)
        vy = sum((y-my)**2 for y in ys)
        corr = sum((x-mx)*(y-my) for x,y in pairs) / math.sqrt(vx*vy) if vx > 0 and vy > 0 else None
        correlations[feature] = round(corr, 4) if corr is not None else None

    result = {
        "experiment": "ats_error_diagnostics_v1",
        "source": str(SOURCE),
        "games": len(rows),
        "definitions": {
            "margin_error": "projected home margin minus actual home margin; positive means model overrated home side",
            "ats_error": "actual home margin plus closing home spread; positive means home beat the spread",
            "edge": "projected home margin plus closing home spread; positive means model prefers home ATS",
            "no thresholds are tuned and no new formula is fitted"
        },
        "overall": summarize(rows),
        "groups": groups,
        "ats_error_correlations": correlations,
        "interpretation_warning": "Descriptive diagnostics only. Associations must be validated prospectively or on a later untouched season before adding features."
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != "groups"}, indent=2))
    print("GROUPS", json.dumps(groups, separators=(",", ":")))


if __name__ == "__main__":
    main()
