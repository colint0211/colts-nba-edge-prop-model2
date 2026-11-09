"""Direct ATS-target reverse engineering: train on 2024-25, test 2025-26.

The response variable is actual home ATS margin (home score - away score +
closing home spread). Inputs include pregame rolling team features, closing
spread, and consensus no-vig home cover probability. The test season is frozen.
"""
from __future__ import annotations
import csv,json,urllib.request
from pathlib import Path
from src.api.nba_historical_backtest import _download,_games,_ridge,FEATURES
from scripts.nba_reverse_engineer_formula import make_examples

OUT=Path("backtest-output/ats_direct_formula.json")
EDGE=2.5
WIN=100/110
URLS={
 2025:"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2024-25/resolve/main/nba_2024_25_closing_lines_ats.csv",
 2026:"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2025-26/resolve/main/nba_2025_26_closing_lines_ats.csv"}
ALIASES={"Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN","Charlotte Hornets":"CHA","Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE","Dallas Mavericks":"DAL","Denver Nuggets":"DEN","Detroit Pistons":"DET","Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND","Los Angeles Clippers":"LAC","LA Clippers":"LAC","Los Angeles Lakers":"LAL","LA Lakers":"LAL","Memphis Grizzlies":"MEM","Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN","New Orleans Pelicans":"NOP","New York Knicks":"NYK","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL","Philadelphia 76ers":"PHI","Philadelphia 76 ers":"PHI","Phoenix Suns":"PHX","Portland Trail Blazers":"POR","Sacramento Kings":"SAC","San Antonio Spurs":"SAS","Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WAS"}
def team(s):
 s=str(s).strip(); return ALIASES.get(s,s.upper().replace(" ",""))
def csv_rows(year):
 req=urllib.request.Request(URLS[year],headers={"User-Agent":"Mozilla/5.0"})
 with urllib.request.urlopen(req,timeout=60) as r:return list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
def build(examples,year):
 lookup={(str(r["date"])[:10],team(r["home_team"]),team(r["away_team"])):r for r in examples}
 out=[]
 for q in csv_rows(year):
  if q.get("season_type")!="regular":continue
  r=lookup.get((q["date_et"][:10],team(q["home_team"]),team(q["away_team"])))
  if r is None:continue
  spread=float(q["close_spread_home"])
  cover_prob=float(q["close_home_cover_prob"])
  x=list(r["features"])+[spread,(cover_prob-.5)*100]
  y=float(r["actual_margin"])+spread
  out.append({"x":x,"y":y,"date":q["date_et"],"home":q["home_team"],"away":q["away_team"],"spread":spread,"actual":y})
 return out
def fit_rows(rows,penalty=2.0):
 n=len(rows[0]["x"]); xtx=[[0.0]*n for _ in range(n)];xty=[0.0]*n
 for r in rows:
  x=r["x"];y=r["y"]
  for i in range(n):
   xty[i]+=x[i]*y
   for j in range(n):xtx[i][j]+=x[i]*x[j]
 return _ridge(xtx,xty,penalty)
def summarize(train,test,beta):
 bets=[]
 for r in test:
  pred=sum(a*b for a,b in zip(r["x"],beta))
  edge=pred
  pick="home" if edge>=EDGE else "away" if edge<=-EDGE else "pass"
  result="pass" if pick=="pass" else "push" if abs(r["actual"])<1e-9 else "win" if ((pick=="home" and r["actual"]>0) or (pick=="away" and r["actual"]<0)) else "loss"
  bets.append({**{k:r[k] for k in ("date","home","away","spread","actual")},"predicted_ats_margin":round(pred,3),"pick":pick,"result":result})
 active=[r for r in bets if r["pick"]!="pass"]
 w=sum(r["result"]=="win" for r in active);l=sum(r["result"]=="loss" for r in active);p=sum(r["result"]=="push" for r in active)
 # Closing-favorite baseline on the same matched game set.
 fw=fl=fp=0
 for r in test:
  m=r["actual"] if r["spread"]<0 else -r["actual"]
  if abs(m)<1e-9:fp+=1
  elif m>0:fw+=1
  else:fl+=1
 units=round(w*WIN-l,2)
 return {"training_rows":len(train),"test_rows":len(test),"matched_test_games":len(test),
  "ats_model":{"bets":len(active),"wins":w,"losses":l,"pushes":p,"win_pct":round(100*w/(w+l),2) if w+l else None,"net_units_at_minus_110":units,"roi_pct":round(100*units/(w+l),2) if w+l else None},
  "closing_favorite_baseline":{"bets":len(test),"wins":fw,"losses":fl,"pushes":fp,"win_pct":round(100*fw/(fw+fl),2) if fw+fl else None,"net_units_at_minus_110":round(fw*WIN-fl,2)},
  "coefficients":beta,"game_results":bets}
def main():
 paths={}
 try:
  for y in (2025,2026):
   p=_download(y);paths[y]=p
  examples={y:make_examples(_games(paths[y])) for y in paths}
 finally:
  for p in paths.values():Path(p).unlink(missing_ok=True)
 train=build(examples[2025],2025)
 test=build(examples[2026],2026)
 if len(train)<500 or len(test)<500:raise RuntimeError(f"Insufficient ATS rows: train={len(train)} test={len(test)}")
 train=sorted(train,key=lambda r:r["date"])
 split=int(len(train)*0.8)
 fit_part,valid_part=train[:split],train[split:]
 penalty_scores=[]
 for penalty in (0.0,0.01,0.1,1.0,2.0,5.0,10.0,25.0,50.0,100.0):
  b=fit_rows(fit_part,penalty)
  mae=sum(abs(sum(a*v for a,v in zip(r["x"],b))-r["y"]) for r in valid_part)/len(valid_part)
  penalty_scores.append({"penalty":penalty,"validation_ats_margin_mae":round(mae,4)})
 chosen=min(penalty_scores,key=lambda x:x["validation_ats_margin_mae"])["penalty"]
 beta=fit_rows(train,chosen)
 res=summarize(train,test,beta)
 result={"experiment":"direct_ats_margin_formula_v1","training_season":"2024-25","untouched_holdout_season":"2025-26",
  "target":"actual home ATS margin against consensus closing spread",
  "inputs":FEATURES+["closing_home_spread","(no_vig_home_cover_probability-0.5)*100"],
  "formula":"ridge regression directly on ATS margin; bet home if predicted ATS margin >= +2.5, away if <= -2.5, otherwise pass",
  "ridge_penalty_selected_on_chronological_training_validation":chosen,"validation_penalty_scores":penalty_scores,"fixed_edge_threshold":EDGE,"break_even_at_minus_110_pct":52.38,
  "results":{k:v for k,v in res.items() if k!="game_results"},
  "coefficients":{name:round(value,8) for name,value in zip(FEATURES+["closing_home_spread","cover_probability_signal"],beta)},
  "integrity":{"holdout_used_for_fitting":False,"threshold_fixed_before_holdout":True,"same_day_result_leakage":False,"live_app_modified":False},
  "game_results":res["game_results"]}
 OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(result,indent=2))
 print(json.dumps({k:v for k,v in result.items() if k!="game_results"},indent=2))
if __name__=="__main__":main()
