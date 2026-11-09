"""Matched-control forensic study of NBA blowouts, road-dog upsets and total collapses.

Reads the verified-source v2 game audit. Uses only pregame columns as explanatory
features. Final scores define event labels but are never used as predictors.
Controls are nearest non-event games from the same season, matched on closing
spread magnitude and closing total. Results are associations, not causal claims.
"""
from __future__ import annotations
import csv, json, math
from datetime import date
from pathlib import Path

ROOT = Path("backtest-output/game_by_game_forensic_audit_v2")
SOURCE = ROOT / "every_game.csv"
OUT = Path("backtest-output/event_factor_study")
OUT.mkdir(parents=True, exist_ok=True)

NUMERIC = [
    "home_team_ortg", "home_team_drtg", "home_team_pace",
    "away_team_ortg", "away_team_drtg", "away_team_pace",
    "home_rest_days", "away_rest_days",
    "projected_margin", "projected_total", "home_ats_edge_points",
    "total_edge_points", "model_margin_sd", "model_total_sd",
    "home_cover_market_prob_no_vig", "over_market_prob_no_vig",
]
def f(row, key):
    try:
        raw = row.get(key, "")
        if raw in ("", None) and key in ("home_rest_days", "away_rest_days"):
            raw = row.get("_calc_" + key, "")
        x = float(raw)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def mean(vals):
    vals = [x for x in vals if x is not None]
    return sum(vals)/len(vals) if vals else None

def sd(vals):
    vals = [x for x in vals if x is not None]
    if len(vals) < 2: return None
    m = mean(vals)
    return (sum((x-m)**2 for x in vals)/(len(vals)-1))**0.5

def score_parts(r):
    margin, total = f(r, "actual_home_margin"), f(r, "actual_total")
    if margin is None or total is None: return None, None
    return (total+margin)/2, (total-margin)/2

def labels(r):
    margin, total = f(r, "actual_home_margin"), f(r, "actual_total")
    spread = f(r, "home_spread")
    closing_total = f(r, "closing_total")
    home_score, away_score = score_parts(r)
    if None in (margin,total,spread,home_score,away_score): return {}
    # Negative home spread means home favourite in the source convention.
    home_fav = spread < 0
    fav_margin = margin if home_fav else -margin
    return {
      "any_20pt_blowout": abs(margin) >= 20,
      "favorite_20pt_blowout": fav_margin >= 20,
      "underdog_20pt_blowout": fav_margin <= -20,
      "road_underdog_upset": home_fav and margin < 0,
      "high_total_bilateral_under100": closing_total is not None and closing_total >= 225 and home_score < 100 and away_score < 100,
    }

def match_distance(a,b):
    # Match market context, not outcomes or model outputs.
    sa, sb = abs(f(a,"home_spread") or 0), abs(f(b,"home_spread") or 0)
    ta, tb = f(a,"closing_total") or 0, f(b,"closing_total") or 0
    return ((sa-sb)/2.5)**2 + ((ta-tb)/10.0)**2

def main():
    if not SOURCE.exists():
        raise FileNotFoundError(f"Required audit not found: {SOURCE}")
    with SOURCE.open(newline="") as fp:
        rows = list(csv.DictReader(fp))
    # Reconstruct rest days from each team's schedule in this audit file.
    # Upstream rest fields were null, so calculate rather than report no data.
    last_game = {}
    for r in sorted(rows, key=lambda x: (x.get("season", ""), x.get("date", ""))):
        season = r.get("season", "")
        try:
            game_day = date.fromisoformat(str(r.get("date", ""))[:10])
        except ValueError:
            r["_calc_home_rest_days"] = ""
            r["_calc_away_rest_days"] = ""
            continue
        for side in ("home", "away"):
            team = r.get(side, "")
            previous = last_game.get((season, team))
            r["_calc_" + side + "_rest_days"] = max(0, (game_day - previous).days - 1) if previous else ""
        for side in ("home", "away"):
            team = r.get(side, "")
            if team:
                last_game[(season, team)] = game_day
    for r in rows:
        r["_labels"] = labels(r)
        r["_season"] = r.get("season","")
    events = ["any_20pt_blowout","favorite_20pt_blowout","underdog_20pt_blowout",
              "road_underdog_upset","high_total_bilateral_under100"]
    results = {"title":"Matched-control pregame factor study",
      "games":len(rows),"events":{},"feature_definitions":NUMERIC,
      "matching":"For each event game, select up to five nearest non-event controls from the same season, using closing spread magnitude and closing total only.",
      "interpretation":"Mean differences are descriptive associations. The closing market is used for matching, so these results do not establish value beyond the market.",
      "data_gaps":["Historical pregame injury status and expected minutes are unavailable.",
       "Actual in-game shooting, turnovers, free throws, offensive rebounds and possession counts are not in this pregame feature file.",
       "Rest days are correctly defined as full calendar days off; zero means a back-to-back."]}
    event_rows=[]
    for label in events:
        group=[r for r in rows if r["_labels"].get(label)]
        contrasts=[]
        for e in group:
            pool=[c for c in rows if c["_season"]==e["_season"] and not c["_labels"].get(label)]
            pool=sorted(pool,key=lambda c:match_distance(e,c))[:5]
            if pool:
                for feature in NUMERIC:
                    ev=f(e,feature)
                    cv=mean([f(c,feature) for c in pool])
                    if ev is not None and cv is not None:
                        contrasts.append((feature,ev-cv,ev,cv))
                event_rows.append({"event":label,"date":e.get("date"),"matchup":e.get("matchup"),
                    "home_spread":e.get("home_spread"),"closing_total":e.get("closing_total"),
                    "actual_home_margin":e.get("actual_home_margin"),"actual_total":e.get("actual_total"),
                    "home_rest_days":e.get("home_rest_days"),"away_rest_days":e.get("away_rest_days"),
                    "matched_controls":len(pool)})
        summary={}
        for feature in NUMERIC:
            vals=[x for x in contrasts if x[0]==feature]
            diffs=[x[1] for x in vals]
            evs=[x[2] for x in vals]; cvs=[x[3] for x in vals]
            summary[feature]={"event_mean":round(mean(evs),4) if evs else None,
              "matched_control_mean":round(mean(cvs),4) if cvs else None,
              "event_minus_control":round(mean(diffs),4) if diffs else None,
              "matched_pairs":len(diffs)}
        results["events"][label]={"count":len(group),
          "season_counts":{s:sum(r["_season"]==s for r in group) for s in sorted({r["_season"] for r in rows})},
          "feature_contrasts":summary}
    with (OUT/"event_games.csv").open("w",newline="") as fp:
        fields=list(event_rows[0]) if event_rows else ["event","date","matchup"]
        w=csv.DictWriter(fp,fieldnames=fields);w.writeheader();w.writerows(event_rows)
    (OUT/"summary.json").write_text(json.dumps(results,indent=2))
    print(json.dumps(results,indent=2))
if __name__=="__main__": main()
