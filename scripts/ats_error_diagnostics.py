#!/usr/bin/env python3
"""Forensic diagnostics for historical NBA ATS predictions (research only).

Requires one row per game, enriched with model probability, team/opponent,
spread and final score. Optional projected_home_margin / projected_total
columns enable continuous margin/total error analysis.
"""
import argparse,csv,json,math,statistics
from collections import defaultdict
from pathlib import Path

def n(x):
 try:
  v=float(x);return v if math.isfinite(v) else None
 except (TypeError,ValueError):return None
def wilson(w,k):
 if not k:return [None,None]
 z=1.96;p=w/k;d=1+z*z/k;c=(p+z*z/(2*k))/d;h=z*math.sqrt(p*(1-p)/k+z*z/(4*k*k))/d
 return [round(max(0,c-h),4),round(min(1,c+h),4)]
def read(path):
 out=[];bad=[]
 with path.open(newline="",encoding="utf-8-sig") as f:
  rd=csv.DictReader(f);required={"game_id","home_team","away_team","home_score","away_score","spread_home","p_home_cover"}
  missing=required-set(rd.fieldnames or [])
  if missing:raise ValueError("Missing columns: "+", ".join(sorted(missing)))
  for ln,r in enumerate(rd,2):
   try:
    vals={k:n(r.get(k)) for k in ("home_score","away_score","spread_home","p_home_cover")}
    if any(v is None for v in vals.values()):raise ValueError("missing/non-numeric score, spread or probability")
    if not 0<=vals["p_home_cover"]<=1:raise ValueError("p_home_cover must be in [0,1]")
    hm=vals["home_score"]-vals["away_score"]; ats=hm+vals["spread_home"]
    result="win" if ats>0 else "loss" if ats<0 else "push"
    row={"game_id":r["game_id"],"home_team":r["home_team"],"away_team":r["away_team"],
      "home_score":vals["home_score"],"away_score":vals["away_score"],"home_margin":hm,
      "spread_home":vals["spread_home"],"p_home_cover":vals["p_home_cover"],"ats_result":result,
      "date":r.get("date_et",""),"projected_home_margin":n(r.get("projected_home_margin")),
      "projected_total":n(r.get("projected_total")),"actual_total":vals["home_score"]+vals["away_score"]}
    row["margin_error"]=row["projected_home_margin"]-hm if row["projected_home_margin"] is not None else None
    row["total_error"]=row["projected_total"]-row["actual_total"] if row["projected_total"] is not None else None
    out.append(row)
   except Exception as e:bad.append({"line":ln,"issue":str(e)})
 if bad:raise ValueError(json.dumps({"invalid_rows":len(bad),"examples":bad[:25]},indent=2))
 ids=[r["game_id"] for r in out]
 if len(ids)!=len(set(ids)):raise ValueError("Duplicate game_id rows; resolve before analysis")
 return out
def summary(rows,min_n):
 k=len(rows);dec=[r for r in rows if r["ats_result"]!="push"];w=sum(r["ats_result"]=="win" for r in dec)
 probs=[r for r in dec];brier=sum((r["p_home_cover"]-(1 if r["ats_result"]=="win" else 0))**2 for r in probs)/len(probs) if probs else None
 errors=[r["margin_error"] for r in rows if r["margin_error"] is not None]
 totals=[r["total_error"] for r in rows if r["total_error"] is not None]
 def err(x):
  if not x:return {"n":0,"bias":None,"mae":None,"rmse":None,"bias_95pct_ci":[None,None]}
  mu=sum(x)/len(x);sd=statistics.stdev(x) if len(x)>1 else 0
  h=1.96*sd/math.sqrt(len(x))
  return {"n":len(x),"bias":round(mu,3),"mae":round(sum(abs(v) for v in x)/len(x),3),
    "rmse":round(math.sqrt(sum(v*v for v in x)/len(x)),3),"bias_95pct_ci":[round(mu-h,3),round(mu+h,3)]}
 return {"games":k,"home_ats_wins":w,"home_ats_losses":len(dec)-w,"pushes":k-len(dec),
  "home_ats_win_rate":round(w/len(dec),4) if dec else None,"home_ats_wilson_95pct":wilson(w,len(dec)),
  "home_cover_brier":round(brier,5) if brier is not None else None,
  "margin_forecast_error":err(errors),"total_forecast_error":err(totals),
  "status":"review_bias_and_validate" if k>=min_n else "small_sample"}
def group(rows,key,min_n):
 d=defaultdict(list)
 for r in rows:
  v=key(r)
  if v not in ("",None,"UNKNOWN"):d[str(v)].append(r)
 return {k:summary(v,min_n) for k,v in sorted(d.items())}
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("csv",type=Path)
 ap.add_argument("--out",type=Path,default=Path("backtest-output/ats_error_diagnostics"));ap.add_argument("--min-group-n",type=int,default=30)
 a=ap.parse_args();rows=read(a.csv)
 if not rows:raise SystemExit("No games to analyze")
 a.out.mkdir(parents=True,exist_ok=True)
 def spread_bucket(r):
  s=abs(r["spread_home"])
  return "<3" if s<3 else "3-5.5" if s<6 else "6-9.5" if s<10 else "10+"
 def prob_bucket(r):
  p=r["p_home_cover"]
  return f"{int(p*10)*10}-{int(p*10)*10+10}%"
 report={"status":"research_only","games":len(rows),"minimum_group_n_for_flags":a.min_group_n,
  "overall":summary(rows,a.min_group_n),
  "by_home_team":group(rows,lambda r:r["home_team"],a.min_group_n),
  "by_away_team":group(rows,lambda r:r["away_team"],a.min_group_n),
  "by_spread_magnitude":group(rows,spread_bucket,a.min_group_n),
  "by_home_cover_probability":group(rows,prob_bucket,a.min_group_n),
  "by_home_favorite_or_underdog":group(rows,lambda r:"home_favorite" if r["spread_home"]<0 else "home_underdog" if r["spread_home"]>0 else "pickem",a.min_group_n),
  "limitations":["Team splits are role-specific: home team and away team are not interchangeable.",
   "A historical segment with a poor record is not proof of repeatable weakness; confidence intervals and multiple-testing risk matter.",
   "Projection margin/total bias is only calculated when those forecasts are supplied; probability-only inputs cannot identify point-margin bias.",
   "All diagnostics are descriptive. Any correction must be pre-specified and tested on a later untouched chronological holdout."]}
 (a.out/"report.json").write_text(json.dumps(report,indent=2))
 with (a.out/"game_errors.csv").open("w",newline="",encoding="utf-8") as f:
  fields=list(rows[0]);w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 print(json.dumps({"games":len(rows),"overall":report["overall"],"output":str(a.out)},indent=2))
if __name__=="__main__":main()
