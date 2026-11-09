"""Frozen, out-of-sample 2026-27 NBA ATS test.

Train the score-margin formula on completed 2024-25 and 2025-26 seasons.
Freeze coefficients and the +/-2.5-point ATS edge rule before evaluating
2026-27 against consensus closing spreads. No holdout fitting or tuning.
"""
from __future__ import annotations
import csv
import json
import urllib.request
from pathlib import Path

from scripts.nba_reverse_engineer_formula import season_rows, make_examples, fit

ODDS_URL = "https://huggingface.co/datasets/minmaxxxer/nba-odds-2026-27/resolve/main/nba_2026_27_odds.csv"
OUT = Path("backtest-output/ats_2026_27_current_season.json")
EDGE = 2.5
WIN = 100.0 / 110.0
ALIASES = {
 "Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN",
 "Charlotte Hornets":"CHA","Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE",
 "Dallas Mavericks":"DAL","Denver Nuggets":"DEN","Detroit Pistons":"DET",
 "Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND",
 "Los Angeles Clippers":"LAC","LA Clippers":"LAC","Los Angeles Lakers":"LAL",
 "LA Lakers":"LAL","Memphis Grizzlies":"MEM","Miami Heat":"MIA",
 "Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN","New Orleans Pelicans":"NOP",
 "New York Knicks":"NYK","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL",
 "Philadelphia 76ers":"PHI","Philadelphia 76 ers":"PHI","Phoenix Suns":"PHX",
 "Portland Trail Blazers":"POR","Sacramento Kings":"SAC","San Antonio Spurs":"SAS",
 "Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WAS"
}

def team_key(value):
    value = str(value or "").strip()
    return ALIASES.get(value, value.upper().replace(" ", ""))

def fetch_odds():
    req = urllib.request.Request(ODDS_URL, headers={"User-Agent":"ColtsNBAHistoricalResearch/1.0"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return list(csv.DictReader(response.read().decode("utf-8-sig").splitlines()))

def summarize(picks):
    wins = sum(x.get("result", x.get("model_result")) == "WIN" for x in picks)
    losses = sum(x.get("result", x.get("model_result")) == "LOSS" for x in picks)
    pushes = sum(x.get("result", x.get("model_result")) == "PUSH" for x in picks)
    units = wins * WIN - losses
    decisions = wins + losses
    return {
        "bets": len(picks), "wins": wins, "losses": losses, "pushes": pushes,
        "win_pct_ex_pushes": round(100*wins/decisions, 2) if decisions else None,
        "net_units_at_assumed_minus_110": round(units, 2),
        "roi_pct_on_units_risked": round(100*units/(wins+losses), 2) if decisions else None,
        "break_even_win_pct_at_minus_110": 52.38
    }

def main():
    # Fit once on two completed seasons, then freeze the formula.
    train = make_examples(season_rows(2025)) + make_examples(season_rows(2026))
    test = make_examples(season_rows(2027))
    if len(train) < 1500 or len(test) < 30:
        raise RuntimeError(f"Insufficient data: train={len(train)} holdout={len(test)}")
    beta = fit(train, "actual_margin")

    feature_lookup = {
        (str(r["date"])[:10], team_key(r["home_team"]), team_key(r["away_team"])): r
        for r in test
    }
    odds = fetch_odds()
    matched = []
    unmatched = []
    for q in odds:
        if str(q.get("season_type", "")).lower() != "regular":
            continue
        date = str(q.get("date_et", ""))[:10]
        key = (date, team_key(q.get("home_team")), team_key(q.get("away_team")))
        r = feature_lookup.get(key)
        if r is None:
            unmatched.append({"date":date,"home":q.get("home_team"),"away":q.get("away_team")})
            continue
        try:
            spread = float(q["close_spread_home"])
            close_home_prob = float(q["close_home_cover_prob"])
            actual_margin = float(r["actual_margin"])
        except (KeyError, TypeError, ValueError):
            continue
        predicted_margin = sum(a*b for a,b in zip(r["features"], beta))
        edge = predicted_margin + spread
        actual_ats = actual_margin + spread
        pick = "HOME" if edge >= EDGE else "AWAY" if edge <= -EDGE else "PASS"
        if pick == "PASS":
            result = "PASS"
        elif abs(actual_ats) < 1e-9:
            result = "PUSH"
        elif (pick == "HOME" and actual_ats > 0) or (pick == "AWAY" and actual_ats < 0):
            result = "WIN"
        else:
            result = "LOSS"
        favorite = "HOME" if spread < 0 else "AWAY" if spread > 0 else "PICKEM"
        fav_result = "PUSH" if abs(actual_ats) < 1e-9 else (
            "WIN" if (favorite == "HOME" and actual_ats > 0) or
            (favorite == "AWAY" and actual_ats < 0) else "LOSS"
        ) if favorite != "PICKEM" else "NOT_GRADED"
        matched.append({
            "date":date,"home":q.get("home_team"),"away":q.get("away_team"),
            "closing_home_spread":spread,"predicted_home_margin":round(predicted_margin,3),
            "model_home_edge_points":round(edge,3),"actual_home_margin":actual_margin,
            "actual_home_ats_margin":round(actual_ats,3),"model_pick":pick,
            "model_result":result,"closing_favorite":favorite,"favorite_result":fav_result,
            "closing_home_cover_probability":close_home_prob
        })

    bets = [x for x in matched if x["model_pick"] != "PASS"]
    favorites = [x for x in matched if x["closing_favorite"] in ("HOME","AWAY")]
    favorite_bets = [
        {"result": x["favorite_result"]} for x in favorites
        if x["favorite_result"] in ("WIN","LOSS","PUSH")
    ]
    report = {
        "experiment":"frozen_2026_27_current_season_ats_holdout",
        "training_seasons":["2024-25","2025-26"],
        "holdout_season":"2026-27",
        "training_games":len(train),"holdout_score_games":len(test),
        "odds_source":ODDS_URL,
        "odds_rows_regular_season":sum(str(x.get("season_type","")).lower()=="regular" for x in odds),
        "matched_games":len(matched),"unmatched_odds_rows":len(unmatched),
        "fixed_rule":f"Home if projected home-margin edge vs closing spread >= +{EDGE}; away if <= -{EDGE}; otherwise pass.",
        "model_ats":summarize(bets),
        "closing_favorite_baseline":summarize(favorite_bets),
        "all_games_home_cover_rate_pct":round(100*sum(x["actual_home_ats_margin"]>0 for x in matched)/len(matched),2) if matched else None,
        "coefficients_frozen_from_prior_seasons":True,
        "integrity":{
            "2026_27_used_for_training":False,
            "threshold_set_before_holdout":True,
            "same_day_result_leakage":False,
            "closing_spread_used_only_to_calculate_edge_not_fit_formula":True,
            "live_app_modified":False
        },
        "limitations":[
            "Consensus closing spread dataset; this is a closing-line diagnostic, not proof the picks were available at an earlier price.",
            "No historical executable DraftKings prices; unit returns assume -110.",
            "No timestamp-verified injury or starting-lineup adjustment is included in this first current-season ATS test.",
            "The 2026-27 sample is small and in progress; do not treat it as a stable long-run estimate."
        ],
        "game_results":matched,
        "unmatched_odds_games":unmatched
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ("game_results","unmatched_odds_games")}, indent=2))
    print("GAME_RESULTS", json.dumps(matched, separators=(",",":")))

if __name__ == "__main__":
    main()
