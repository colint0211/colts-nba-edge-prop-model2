"""Multi-season leakage-safe reverse-engineered NBA ATS backtest.

Fit the margin formula only on seasons before each holdout. Evaluate against
consensus closing spreads with a fixed +/-2.5-point edge threshold at -110.
No holdout threshold tuning and no live-model changes.
"""
from __future__ import annotations
import csv, json, urllib.request
from pathlib import Path
from src.api.nba_historical_backtest import _download, _games, FEATURES
from scripts.nba_reverse_engineer_formula import make_examples, fit

OUT=Path("backtest-output/ats_multiseason.json")
EDGE=2.5
WIN=100/110
ALIASES={
 "Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN","Charlotte Hornets":"CHA",
 "Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE","Dallas Mavericks":"DAL","Denver Nuggets":"DEN",
 "Detroit Pistons":"DET","Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND",
 "Los Angeles Clippers":"LAC","LA Clippers":"LAC","Los Angeles Lakers":"LAL","LA Lakers":"LAL",
 "Memphis Grizzlies":"MEM","Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN",
 "New Orleans Pelicans":"NOP","New York Knicks":"NYK","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL",
 "Philadelphia 76ers":"PHI","Philadelphia 76 ers":"PHI","Phoenix Suns":"PHX","Portland Trail Blazers":"POR",
 "Sacramento Kings":"SAC","San Antonio Spurs":"SAS","Toronto Raptors":"TOR","Utah Jazz":"UTA",
 "Washington Wizards":"WAS"}
URLS={
 2025:"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2024-25/resolve/main/nba_2024_25_closing_lines_ats.csv",
 2026:"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2025-26/resolve/main/nba_2025_26_closing_lines_ats.csv"}
def key(s):
 s=str(s).strip()
 return ALIASES.get(s,s.upper().replace(" ",""))
def fetch_csv(url):
 req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
 with urllib.request.urlopen(req,timeout=60) as r:
  return list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
def evaluate(train,test,year):
 beta=fit(train,"actual_margin")
 odds=fetch_csv(URLS[year])
 lookup={(str(x["date"])[:10],key(x["home_team"]),key(x["away_team"])):x for x in test}
 rows=[]
 for q in odds:
  if q.get("season_type")!="regular": continue
  k=(q["date_et"][:10],key(q["home_team"]),key(q["away_team"]))
  r=lookup.get(k)
  if r is None: continue
  spread=float(q["close_spread_home"])
  pred=sum(a*b for a,b in zip(r["features"],beta))
  edge=pred+spread
  actual=float(r["actual_margin"])+spread
  pick="home" if edge>=EDGE else "away" if edge<=-EDGE else "pass"
  result="pass" if pick=="pass" else "push" if abs(actual)<1e-9 else "win" if ((pick=="home" and actual>0) or (pick=="away" and actual<0)) else "loss"
  rows.append({"date":q["date_et"],"home":q["home_team"],"away":q["away_team"],
   "close_spread_home":spread,"predicted_home_margin":round(pred,3),
   "model_edge_home_points":round(edge,3),"actual_ats_margin_home":round(actual,3),
   "pick":pick,"result":result})
 bets=[r for r in rows if r["pick"]!="pass"]
 w=sum(r["result"]=="win" for r in bets); l=sum(r["result"]=="loss" for r in bets); p=sum(r["result"]=="push" for r in bets)
 units=round(w*WIN-l,2)
 fav=[r for r in rows if r["close_spread_home"]!=0]
 fw=fl=fp=0
 for r in fav:
  m=r["actual_ats_margin_home"] if r["close_spread_home"]<0 else -r["actual_ats_margin_home"]
  if abs(m)<1e-9: fp+=1
  elif m>0: fw+=1
  else: fl+=1
 return {"holdout_season":f"{year-1}-{str(year)[-2:]}","training_seasons":[f"{y-1}-{str(y)[-2:]}" for y in ([2024] if year==2025 else [2024,2025])],
  "training_games":len(train),"holdout_score_games":len(test),"matched_games":len(rows),
  "model_ats":{"bets":len(bets),"wins":w,"losses":l,"pushes":p,
   "win_pct_ex_pushes":round(100*w/(w+l),2) if w+l else None,"net_units_at_minus_110":units,
   "roi_pct":round(100*units/(w+l),2) if w+l else None},
  "closing_favorite_baseline":{"bets":len(fav),"wins":fw,"losses":fl,"pushes":fp,
   "win_pct_ex_pushes":round(100*fw/(fw+fl),2) if fw+fl else None,
   "net_units_at_minus_110":round(fw*WIN-fl,2)},
  "coefficients":{k:round(v,8) for k,v in zip(FEATURES,beta)},
  "game_results":rows}
def main():
 data={}
 for y in (2024,2025,2026):
  p=_download(y)
  try: data[y]=make_examples(_games(p))
  finally: Path(p).unlink(missing_ok=True)
 if min(map(len,data.values()))<500: raise RuntimeError("Insufficient season feature rows")
 r1=evaluate(data[2024],data[2025],2025)
 r2=evaluate(data[2024]+data[2025],data[2026],2026)
 result={"experiment":"reverse_engineered_ats_multiseason_v1","fixed_edge_threshold_points":EDGE,
  "odds":"consensus closing spread; -110 flat stake accounting",
  "formula":"ridge regression trained on prior-season pregame rolling offense/defense, pace, rest and back-to-back features; home ATS edge = projected home margin + closing home spread",
  "seasons":[r1,r2],
  "integrity":{"holdout_seasons_excluded_from_fit":True,"threshold_fixed_before_each_holdout":True,
   "same_day_result_leakage":False,"closing_spread_not_used_as_model_feature":True,"live_app_modified":False}}
 OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(result,indent=2))
 print(json.dumps({**result,"seasons":[{k:v for k,v in s.items() if k!="game_results"} for s in result["seasons"]]},indent=2))
if __name__=="__main__": main()
