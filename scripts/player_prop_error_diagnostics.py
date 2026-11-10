#!/usr/bin/env python3
"""Forensic diagnostics for settled NBA player-prop predictions (research only).

Input: CSV export of settled nba_predictions rows. The snapshot JSON is used to
recover opponent and other point-in-time fields where present. No predictions
are edited and no rows are silently discarded.
"""
import argparse, csv, json, math, statistics
from collections import defaultdict
from pathlib import Path

def num(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError): return None

def wilson(w,n,z=1.96):
    if not n:return [None,None]
    p=w/n; d=1+z*z/n
    c=(p+z*z/(2*n))/d
    h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [round(max(0,c-h),4),round(min(1,c+h),4)]

def read(path):
    rows=[]; issues=[]
    with path.open(newline="",encoding="utf-8-sig") as f:
        rd=csv.DictReader(f)
        required={"player","market","line","projection","actual_value","outcome"}
        missing=required-set(rd.fieldnames or [])
        if missing: raise ValueError("Missing columns: "+", ".join(sorted(missing)))
        for ln,r in enumerate(rd,2):
            try:
                snap=json.loads(r.get("snapshot") or "{}")
                if not isinstance(snap,dict): snap={}
                projection=num(r.get("projection") or snap.get("projection"))
                actual=num(r.get("actual_value"))
                line=num(r.get("line"))
                if not r.get("player","").strip(): raise ValueError("missing player")
                if not r.get("market","").strip(): raise ValueError("missing market")
                if None in (projection,actual,line): raise ValueError("missing/non-numeric projection, actual_value or line")
                outcome=(r.get("outcome") or "").upper()
                if outcome not in ("WIN","LOSS","PUSH"): raise ValueError("invalid/unsettled outcome")
                side=(r.get("side") or snap.get("best_side") or "").upper()
                opponent=(r.get("opponent") or snap.get("opponent") or snap.get("opp") or "UNKNOWN").strip()
                team=(r.get("team") or snap.get("team") or "UNKNOWN").strip()
                probability=num(r.get("blended_probability") or r.get("model_probability"))
                if probability is not None and probability>1: probability/=100
                rows.append({"player":r["player"].strip(),"team":team,"opponent":opponent,
                    "market":r["market"].strip().lower(),"projection":projection,"actual":actual,
                    "line":line,"side":side,"outcome":outcome,"probability":probability,
                    "date":r.get("created_at") or r.get("settled_at") or "",
                    "error":projection-actual,"abs_error":abs(projection-actual),
                    "snapshot":snap})
            except Exception as e: issues.append({"line":ln,"issue":str(e)})
    if issues: raise ValueError(json.dumps({"invalid_rows":len(issues),"examples":issues[:30]},indent=2))
    return rows

def summarize(rows,min_n):
    n=len(rows); errors=[r["error"] for r in rows]
    wins=sum(r["outcome"]=="WIN" for r in rows); losses=sum(r["outcome"]=="LOSS" for r in rows)
    pushes=sum(r["outcome"]=="PUSH" for r in rows)
    decided=wins+losses
    bias=sum(errors)/n if n else None
    sd=statistics.stdev(errors) if n>1 else None
    ci=[bias-1.96*sd/math.sqrt(n),bias+1.96*sd/math.sqrt(n)] if n>1 else [None,None]
    probs=[r for r in rows if r["probability"] is not None and r["outcome"]!="PUSH"]
    brier=sum((r["probability"]-(1 if r["outcome"]=="WIN" else 0))**2 for r in probs)/len(probs) if probs else None
    return {"n":n,"mean_signed_error_projection_minus_actual":round(bias,4) if bias is not None else None,
      "bias_95pct_ci":[round(x,4) if x is not None else None for x in ci],
      "mae":round(sum(abs(x) for x in errors)/n,4) if n else None,
      "rmse":round(math.sqrt(sum(x*x for x in errors)/n),4) if n else None,
      "wins":wins,"losses":losses,"pushes":pushes,
      "hit_rate":round(wins/decided,4) if decided else None,
      "hit_rate_wilson_95pct":wilson(wins,decided),
      "brier":round(brier,5) if brier is not None else None,
      "probability_sample":len(probs),
      "diagnostic_status":"review_bias_and_validate" if n>=min_n and ci[0] is not None and (ci[0]>0 or ci[1]<0) else ("small_sample" if n<min_n else "no_clear_bias")}

def bucket(r):
    p=r["projection"]
    return ("<5" if p<5 else "5-9.9" if p<10 else "10-14.9" if p<15 else "15-19.9" if p<20 else "20-24.9" if p<25 else "25+")
def build(rows,key,min_n):
    groups=defaultdict(list)
    for r in rows:
        val=key(r)
        if val not in ("",None,"UNKNOWN"): groups[str(val)].append(r)
    return {k:summarize(v,min_n) for k,v in sorted(groups.items())}
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv",type=Path); ap.add_argument("--out",type=Path,default=Path("backtest-output/player_prop_diagnostics"))
    ap.add_argument("--min-group-n",type=int,default=30)
    a=ap.parse_args()
    if a.min_group_n<2: ap.error("--min-group-n must be >=2")
    rows=read(a.csv)
    if not rows: raise SystemExit("No settled prediction rows found")
    a.out.mkdir(parents=True,exist_ok=True)
    report={"status":"research_only","source_rows":len(rows),"minimum_group_n_for_flags":a.min_group_n,
      "overall":summarize(rows,a.min_group_n),
      "by_player":build(rows,lambda r:r["player"],a.min_group_n),
      "by_team":build(rows,lambda r:r["team"],a.min_group_n),
      "by_opponent":build(rows,lambda r:r["opponent"],a.min_group_n),
      "by_market":build(rows,lambda r:r["market"],a.min_group_n),
      "by_projection_range":build(rows,bucket,a.min_group_n),
      "by_betting_line":build(rows,lambda r:("int " if abs(r["line"]-round(r["line"]))<1e-8 else "half ") + str(math.floor(r["line"])),a.min_group_n),
      "limitations":["Opponent/team breakdowns exclude UNKNOWN values rather than guessing.",
      "Bias confidence intervals are normal-approximation diagnostics; repeated subgroup testing creates false positives.",
      "The CSV must contain one immutable first-seen prediction per intended evaluation unit; repeated odds refreshes are not independent samples.",
      "A significant historical bias is a hypothesis, not permission to change the projection formula. Validate any correction on later untouched dates."]}
    (a.out/"report.json").write_text(json.dumps(report,indent=2))
    # Row-level residual file makes every error auditable.
    with (a.out/"prediction_errors.csv").open("w",newline="",encoding="utf-8") as f:
        fields=["player","team","opponent","market","projection","actual","line","side","outcome","error","abs_error","probability","date"]
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in rows:w.writerow({k:r.get(k) for k in fields})
    print(json.dumps({"rows":len(rows),"overall":report["overall"],"output":str(a.out)},indent=2))
if __name__=="__main__":main()
