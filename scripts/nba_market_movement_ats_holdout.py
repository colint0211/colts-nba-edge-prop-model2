"""Frozen market-movement model tested on untouched 2026-27 NBA games."""
from __future__ import annotations
import csv,json,math,urllib.request
from pathlib import Path

OUT=Path("backtest-output/market_movement_ats_holdout")
OUT.mkdir(parents=True,exist_ok=True)
URLS={
 "2024-25":"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2024-25/resolve/main/nba_2024_25_closing_lines_ats.csv",
 "2025-26":"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2025-26/resolve/main/nba_2025_26_closing_lines_ats.csv",
 "2026-27":"https://huggingface.co/datasets/minmaxxxer/nba-odds-2026-27/resolve/main/nba_2026_27_odds.csv",
}
FEATURES=["spread_move_toward_home","home_win_probability_move","total_move"]

def load(season):
 req=urllib.request.Request(URLS[season],headers={"User-Agent":"ColtsNBAMarketMoveResearch/1.0"})
 with urllib.request.urlopen(req,timeout=120) as r: rows=list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
 out=[]
 for x in rows:
  if str(x.get("season_type","")).lower()!="regular":continue
  try:
   hs=float(x["home_score"]); aw=float(x["away_score"]); close=float(x["close_spread_home"])
   op=float(x["open_spread_home"]); openp=float(x["open_home_win_prob"]); closep=float(x["close_home_win_prob"])
   ot=float(x["open_total"]); ct=float(x["close_total"])
  except (ValueError,KeyError,TypeError):continue
  out.append({"season":season,"date":x.get("date_et"),"game_id":x.get("game_id"),
    "matchup":str(x.get("away_team"))+" @ "+str(x.get("home_team")),
    "ats":hs-aw+close,"open_ats":hs-aw+op,
    "features":{"spread_move_toward_home":op-close,
                "home_win_probability_move":closep-openp,"total_move":ct-ot}})
 return out

def solve(a,b):
 n=len(b);m=[list(a[i])+[b[i]] for i in range(n)]
 for c in range(n):
  p=max(range(c,n),key=lambda r:abs(m[r][c]));m[c],m[p]=m[p],m[c]
  if abs(m[c][c])<1e-10:m[c][c]+=1e-8
  z=m[c][c]
  for j in range(c,n+1):m[c][j]/=z
  for r in range(n):
   if r==c:continue
   z=m[r][c]
   for j in range(c,n+1):m[r][j]-=z*m[c][j]
 return [m[i][n] for i in range(n)]

def fit(rows):
 means=[];scales=[]
 for f in FEATURES:
  v=[r["features"][f] for r in rows];mu=sum(v)/len(v)
  sd=math.sqrt(sum((x-mu)**2 for x in v)/max(1,len(v)-1)) or 1
  means.append(mu);scales.append(sd)
 ym=sum(r["ats"] for r in rows)/len(rows);p=len(FEATURES)
 a=[[0.]*(p+1) for _ in range(p+1)];b=[0.]*(p+1)
 for r in rows:
  x=[1.]+[(r["features"][f]-means[i])/scales[i] for i,f in enumerate(FEATURES)]
  y=r["ats"]-ym
  for i in range(p+1):
   b[i]+=x[i]*y
   for j in range(p+1):a[i][j]+=x[i]*x[j]
 for i in range(1,p+1):a[i][i]+=10.
 return {"means":means,"scales":scales,"ym":ym,"beta":solve(a,b)}

def predict(m,r):
 x=[1.]+[(r["features"][f]-m["means"][i])/m["scales"][i] for i,f in enumerate(FEATURES)]
 return m["ym"]+sum(x[i]*m["beta"][i] for i in range(len(x)))

def eval_model(train,test,label):
 m=fit(train);res=[r["ats"]-predict(m,r) for r in train]
 sigma=math.sqrt(sum(e*e for e in res)/max(1,len(res)-1))
 scale=max(1,sigma*math.sqrt(3)/math.pi)
 out={"train":label,"train_games":len(train),"test_games":len(test),
      "train_residual_sd":round(sigma,3),
      "coefficients_standardized":{f:round(m["beta"][i+1],4) for i,f in enumerate(FEATURES)},
      "thresholds":{}}
 for t in (.55,.60):
  picks=[]
  for r in test:
   edge=predict(m,r);p=1/(1+math.exp(-max(-30,min(30,edge/scale))))
   if p>=t:side="HOME"
   elif p<=1-t:side="AWAY"
   else:continue
   win=(r["ats"]>0) if side=="HOME" else (r["ats"]<0)
   if abs(r["ats"])<1e-9:continue
   picks.append((side,win,edge,p,r))
  w=sum(x[1] for x in picks);l=len(picks)-w;n=len(picks);u=w*100/110-l
  out["thresholds"][str(t)]={"bets":n,"wins":w,"losses":l,
    "win_pct":round(100*w/n,2) if n else None,
    "units_at_minus_110":round(u,2),"roi_pct":round(100*u/n,2) if n else None,
    "sample_matchups":[{"side":x[0],"win":x[1],"edge":round(x[2],2),"p_home":round(x[3],3),
       "date":x[4]["date"],"matchup":x[4]["matchup"]} for x in picks[:12]]}
 return out

def main():
 a=load("2024-25");b=load("2025-26");c=load("2026-27")
 # Chronological first holdout and untouched current-season holdout.
 first=eval_model(a,b,"2024-25")
 current=eval_model(a+b,c,"2024-25 + 2025-26")
 # Simple descriptive opening-line versus closing-line ATS baselines.
 def rec(rows,key):
  vals=[r[key] for r in rows if abs(r[key])>1e-9]
  w=sum(v>0 for v in vals);l=sum(v<0 for v in vals)
  return {"bets":len(vals),"wins":w,"losses":l,"win_pct":round(100*w/len(vals),2) if vals else None}
 result={"experiment":"frozen_market_movement_ats_v1",
  "features":FEATURES,
  "rule":"Ridge predicts home ATS margin at closing line from opening-to-closing spread, moneyline-probability, and total movement. Home/away bets at calibrated 55% or 60% probability. No threshold tuning on test seasons.",
  "calibration":"Residual standard deviation estimated on training data; flat -110 assumed.",
  "chronological_2024_25_to_2025_26":first,
  "untouched_2026_27_holdout":current,
  "2026_27_market_baselines":{"opening_line_home_cover":rec(c,"open_ats"),"closing_line_home_cover":rec(c,"ats")},
  "coverage":{"2024-25":len(a),"2025-26":len(b),"2026-27":len(c)},
  "integrity":{"test_2026_27_used_in_training":False,"post_tipoff_data_used_as_features":False,
               "production_deployed":False,"live_model_modified":False}}
 (OUT/"market_movement_ats_holdout.json").write_text(json.dumps(result,indent=2))
 print(json.dumps(result,indent=2))

if __name__=="__main__":main()
