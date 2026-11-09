"""Leakage-safe ATS test: train score formula on 2023-24, test 2024-25 closing spreads.

The model is frozen before the test season. A bet is made only when projected
home-margin edge versus the consensus closing spread is >= 2.5 points.
One unit risked at -110; no threshold tuning on the holdout.
"""
from __future__ import annotations
import csv, json, math, urllib.request
from pathlib import Path
from src.api.nba_historical_backtest import _download, _games, _ridge, FEATURES
from scripts.nba_reverse_engineer_formula import make_examples, fit

ODDS_URL = "https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2024-25/resolve/main/nba_2024_25_closing_lines_ats.csv"
OUT = Path("backtest-output/ats_reverse_engineering.json")
EDGE_THRESHOLD = 2.5
RISK = 1.0
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
 "Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WAS",
}
def team_key(s):
    s=str(s).strip()
    return ALIASES.get(s, s.upper().replace(" ", ""))

def download_odds():
    req=urllib.request.Request(ODDS_URL,headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req,timeout=60) as r:
        return list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))

def main():
    train_path=_download(2024)
    test_path=_download(2025)
    try:
        train=make_examples(_games(train_path))
        test=make_examples(_games(test_path))
    finally:
        Path(train_path).unlink(missing_ok=True); Path(test_path).unlink(missing_ok=True)
    if len(train)<500 or len(test)<500:
        raise RuntimeError(f"Insufficient score samples train={len(train)} test={len(test)}")
    beta=fit(train,"actual_margin")
    odds=download_odds()
    # Only regular-season games; key by date and team identity.
    lookup={}
    for r in test:
        # Source game rows use the NBA team abbreviations as team keys.
        lookup[(str(r["date"])[:10],team_key(r.get("home_team","")),team_key(r.get("away_team","")))]=r
    # Rebuild keyed score examples directly from feature rows. make_examples stores
    # only features/date, so join against the source odds via score+date+teams below.
    # Reconstruct features from the season examples by matching score and date/team
    # using a deterministic second pass over the original season dataset.
    # Use a source-agnostic key from game results to avoid game-id differences.
    score_index={}
    for r in test:
        score_index.setdefault((str(r["date"])[:10],round(r["actual_margin"],1),round(r["actual_total"],1)),[]).append(r)
    matched=[]
    for q in odds:
        if q.get("season_type")!="regular": continue
        date=q["date_et"][:10]
        margin=float(q["home_score"])-float(q["away_score"])
        total=float(q["home_score"])+float(q["away_score"])
        candidates=score_index.get((date,round(margin,1),round(total,1)),[])
        if not candidates: continue
        r=candidates.pop(0)
        spread=float(q["close_spread_home"])
        pred=sum(a*b for a,b in zip(r["features"],beta))
        edge=pred+spread
        outcome=margin+spread
        pick="home" if edge>=EDGE_THRESHOLD else ("away" if edge<=-EDGE_THRESHOLD else "pass")
        result="push" if abs(outcome)<1e-9 else ("win" if (pick=="home" and outcome>0) or (pick=="away" and outcome<0) else "loss") if pick!="pass" else "pass"
        matched.append({"date":date,"home":q["home_team"],"away":q["away_team"],
            "close_spread_home":spread,"predicted_home_margin":round(pred,3),
            "model_edge_home_points":round(edge,3),"actual_ats_margin_home":round(outcome,3),
            "pick":pick,"result":result})
    bets=[x for x in matched if x["pick"]!="pass"]
    wins=sum(x["result"]=="win" for x in bets); losses=sum(x["result"]=="loss" for x in bets); pushes=sum(x["result"]=="push" for x in bets)
    units=round(wins*WIN-losses*RISK,2)
    # Baseline: take the closing favorite on every matched game.
    fav_wins=fav_losses=fav_pushes=0
    for x in matched:
        fav="home" if x["close_spread_home"]<0 else "away"
        m=x["actual_ats_margin_home"] if fav=="home" else -x["actual_ats_margin_home"]
        if abs(m)<1e-9: fav_pushes+=1
        elif m>0: fav_wins+=1
        else: fav_losses+=1
    fav_units=round(fav_wins*WIN-fav_losses*RISK,2)
    result={
      "experiment":"reverse_engineered_ats_formula_v1",
      "training_season":"2023-24","holdout_season":"2024-25",
      "training_games":len(train),"holdout_score_games":len(test),
      "closing_line_source":"Hugging Face minmaxxxer/nba-closing-lines-ats-2024-25; consensus closing spread",
      "matched_games":len(matched),"regular_season_only":True,
      "formula":"ridge regression of pregame rolling offense/defense efficiency, pace, rest and back-to-back features to predict home margin; ATS edge = predicted home margin + closing home spread",
      "ridge_penalty":2.0,"features":FEATURES,
      "coefficients":{k:round(v,8) for k,v in zip(FEATURES,beta)},
      "fixed_bet_rule":f"Bet home if edge >= {EDGE_THRESHOLD} points; away if edge <= -{EDGE_THRESHOLD}; otherwise pass",
      "model_ats":{"bets":len(bets),"wins":wins,"losses":losses,"pushes":pushes,
        "win_pct_ex_pushes":round(100*wins/(wins+losses),2) if wins+losses else None,
        "units_risked":round((wins+losses)*RISK,2),"net_units":units,
        "roi_pct":round(100*units/((wins+losses)*RISK),2) if wins+losses else None,
        "break_even_pct_at_minus_110":52.38},
      "closing_favorite_baseline":{"bets":len(matched),"wins":fav_wins,"losses":fav_losses,"pushes":fav_pushes,
        "win_pct_ex_pushes":round(100*fav_wins/(fav_wins+fav_losses),2) if fav_wins+fav_losses else None,
        "net_units_at_minus_110":fav_units},
      "integrity":{"holdout_used_for_fitting":False,"threshold_set_before_holdout":True,
        "same_day_result_leakage":False,"closing_line_used_as_benchmark_not_predictor":True,
        "no_live_app_changes":True},
      "game_results":matched
    }
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="game_results"},indent=2))
    print("MATCHED_GAME_ROWS",len(matched))
if __name__=="__main__": main()
