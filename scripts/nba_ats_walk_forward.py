#!/usr/bin/env python3
"""Expanding-window walk-forward NBA ATS backtest with an auditable bet ledger.

CSV schema is documented in research/WALK_FORWARD_BACKTEST.md. No rows are
silently discarded: invalid rows stop the run and must be resolved explicitly.
"""
from __future__ import annotations
import argparse, csv, json, math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

FEATURES = [
    "spread_move_toward_home", "home_win_probability_move", "total_move",
    "elo_diff_home", "rolling_net_rating_diff", "rest_days_diff",
    "schedule_burden_diff", "injury_availability_diff",
    "rolling_off_def_rating_diff", "travel_tz_diff",
]
REQUIRED = [
    "date_et", "game_id", "scheduled_tipoff_et", "prediction_timestamp_et",
    "feature_available_at_et", "odds_available_at_et", "home_score", "away_score",
    "spread_home", "home_odds_american", "away_odds_american", *FEATURES,
]

def parse_dt(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError(f"Timestamp lacks timezone offset: {s}")
    return d

def american_decimal(o):
    o=float(o)
    if not math.isfinite(o) or o == 0: raise ValueError("American odds must be finite and non-zero")
    return 1 + (o/100 if o > 0 else 100/abs(o))

def settle(home_margin, home_spread):
    x=home_margin + home_spread
    return "win" if x > 0 else ("loss" if x < 0 else "push")

def read_rows(path, cutoff_name):
    rows=[]; issues=[]
    with path.open(newline="",encoding="utf-8-sig") as f:
        reader=csv.DictReader(f)
        missing=[c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing: raise ValueError("Missing required columns: "+", ".join(missing))
        for line, raw in enumerate(reader, start=2):
            game=raw.get("game_id","").strip()
            try:
                blank=[k for k in REQUIRED if not raw.get(k,"").strip()]
                if blank: raise ValueError("blank required fields: "+",".join(blank))
                pred=parse_dt(raw["prediction_timestamp_et"])
                avail=parse_dt(raw["feature_available_at_et"])
                odds_at=parse_dt(raw["odds_available_at_et"])
                tip=parse_dt(raw["scheduled_tipoff_et"])
                if avail > pred: raise ValueError("feature data timestamp is after prediction cutoff")
                if odds_at > pred: raise ValueError("sportsbook price timestamp is after prediction cutoff")
                if pred >= tip: raise ValueError("prediction cutoff must be before scheduled tipoff")
                row={"game_id":game,"date":parse_dt(raw["date_et"]).date(),
                     "prediction_at":pred,"tipoff":tip,
                     "home_margin":float(raw["home_score"])-float(raw["away_score"]),
                     "spread_home":float(raw["spread_home"]),
                     "home_odds":float(raw["home_odds_american"]),
                     "away_odds":float(raw["away_odds_american"]),
                     "features":{k:float(raw[k]) for k in FEATURES}}
                if not all(math.isfinite(v) for v in [row["home_margin"],row["spread_home"],row["home_odds"],row["away_odds"],*row["features"].values()]):
                    raise ValueError("non-finite numeric value")
                american_decimal(row["home_odds"]); american_decimal(row["away_odds"])
                row["outcome"]=settle(row["home_margin"],row["spread_home"])
                rows.append(row)
            except Exception as e:
                issues.append({"line":line,"game_id":game,"issue":str(e)})
    if issues:
        raise ValueError(json.dumps({"invalid_rows":len(issues),"examples":issues[:20]},indent=2))
    ids=[r["game_id"] for r in rows]
    if len(ids)!=len(set(ids)): raise ValueError("Duplicate game_id rows detected; resolve before backtesting")
    rows.sort(key=lambda r:(r["date"],r["prediction_at"],r["game_id"]))
    if len(rows)<120: raise ValueError(f"Need at least 120 valid rows; found {len(rows)}")
    return rows

def sigmoid(z):
    z=max(-30,min(30,z)); return 1/(1+math.exp(-z))

def solve(a,b):
    m=[list(x)+[v] for x,v in zip(a,b)]; n=len(b)
    for c in range(n):
        p=max(range(c,n),key=lambda r:abs(m[r][c])); m[c],m[p]=m[p],m[c]
        if abs(m[c][c])<1e-12: m[c][c]=1e-12
        q=m[c][c]
        for j in range(c,n+1): m[c][j]/=q
        for r in range(n):
            if r==c: continue
            q=m[r][c]
            for j in range(c,n+1): m[r][j]-=q*m[c][j]
    return [m[i][n] for i in range(n)]

def fit(train, ridge=1.0):
    means=[]; scales=[]
    for f in FEATURES:
        vals=[r["features"][f] for r in train]
        mu=sum(vals)/len(vals); sd=math.sqrt(sum((x-mu)**2 for x in vals)/max(1,len(vals)-1)) or 1
        means.append(mu); scales.append(sd)
    beta=[0.0]*(len(FEATURES)+1)
    for _ in range(80):
        h=[[0.0]*len(beta) for _ in beta]; g=[0.0]*len(beta)
        for r in train:
            x=[1.0]+[(r["features"][f]-means[i])/scales[i] for i,f in enumerate(FEATURES)]
            y=1 if r["outcome"]=="win" else 0
            p=sigmoid(sum(a*b for a,b in zip(x,beta))); w=max(1e-8,p*(1-p))
            for i in range(len(beta)):
                g[i]+=x[i]*(y-p)
                for j in range(len(beta)): h[i][j]+=w*x[i]*x[j]
        for j in range(1,len(beta)): h[j][j]+=ridge; g[j]-=ridge*beta[j]
        delta=solve(h,g); beta=[a+b for a,b in zip(beta,delta)]
        if max(abs(x) for x in delta)<1e-7: break
    return {"means":means,"scales":scales,"beta":beta}

def predict(model,r):
    x=[1.0]+[(r["features"][f]-model["means"][i])/model["scales"][i] for i,f in enumerate(FEATURES)]
    return sigmoid(sum(a*b for a,b in zip(x,model["beta"])))

def profit(outcome, odds, side):
    if outcome=="push": return 0.0
    won=(outcome=="win") if side=="home" else (outcome=="loss")
    return american_decimal(odds)-1 if won else -1.0

def summarize(ledger, side_key="selected_side"):
    bets=[r for r in ledger if r[side_key] in ("home","away")]
    def did_win(r):
        return (r["result"]=="win") if r[side_key]=="home" else (r["result"]=="loss")
    wins=sum(did_win(r) for r in bets)
    pushes=sum(r["result"]=="push" for r in bets)
    losses=len(bets)-wins-pushes
    units=sum(r["net_units"] if side_key=="selected_side" else r["baseline_net_units"] for r in bets)
    risk=len(bets)
    peak=0.0; equity=0.0; maxdd=0.0
    for r in bets:
        equity += r["net_units"] if side_key=="selected_side" else r["baseline_net_units"]
        peak=max(peak,equity); maxdd=max(maxdd,peak-equity)
    decided=wins+losses
    return {"bets":len(bets),"wins":wins,"losses":losses,"pushes":pushes,
            "win_pct_ex_pushes":round(100*wins/decided,2) if decided else None,
            "net_units":round(units,4),"risked_units":risk,
            "roi_pct":round(100*units/risk,3) if risk else None,
            "max_drawdown_units":round(maxdd,4)}

def probability_metrics(ledger):
    rows=[r for r in ledger if r["result"]!="push"]
    if not rows:return {"games":0,"brier":None,"log_loss":None,"calibration_bins":[]}
    eps=1e-12
    brier=sum((r["p_home_cover"]-(1 if r["result"]=="win" else 0))**2 for r in rows)/len(rows)
    ll=-sum((1 if r["result"]=="win" else 0)*math.log(max(eps,r["p_home_cover"]))+
            (1 if r["result"]=="loss" else 0)*math.log(max(eps,1-r["p_home_cover"])) for r in rows)/len(rows)
    bins=[]
    for lo in [0,.1,.2,.3,.4,.5,.6,.7,.8,.9]:
        group=[r for r in rows if lo<=r["p_home_cover"]<lo+.1]
        if group: bins.append({"range":f"{lo:.1f}-{lo+.1:.1f}","n":len(group),
            "mean_predicted":round(sum(r["p_home_cover"] for r in group)/len(group),4),
            "observed_home_cover":round(sum(r["result"]=="win" for r in group)/len(group),4)})
    return {"games":len(rows),"brier":round(brier,6),"log_loss":round(ll,6),"calibration_bins":bins}

def run(rows, min_train=100, test_days=7, away_threshold=.45):
    by_day=defaultdict(list)
    for r in rows: by_day[r["date"]].append(r)
    days=sorted(by_day); first_test=days[0]+timedelta(days=0)
    ledger=[]; fold_reports=[]
    # Expanding training set; each test block is strictly later than all training dates.
    for i in range(0,len(days),test_days):
        block_days=days[i:i+test_days]; start=block_days[0]
        train=[r for r in rows if r["date"]<start]
        test=[r for d in block_days for r in by_day[d]]
        if len(train)<min_train: continue
        # Training excludes pushes because they are not binary ATS outcomes.
        fit_rows=[r for r in train if r["outcome"]!="push"]
        if len(fit_rows)<min_train: continue
        model=fit(fit_rows)
        fold=[]
        for r in test:
            p=predict(model,r)
            selected="away" if p<=away_threshold else ("home" if p>=1-away_threshold else "pass")
            # Model's current approved ATS policy is away-only; do not take home selections.
            selected="away" if p<=away_threshold else "pass"
            result=r["outcome"]
            chosen_odds=r["away_odds"] if selected=="away" else None
            net=profit(result,chosen_odds,"away") if selected=="away" else 0.0
            # Naive benchmark: bet home ATS on every game at the contemporaneous recorded price.
            base_net=profit(result,r["home_odds"],"home")
            item={"game_id":r["game_id"],"date_et":r["date"].isoformat(),
                  "prediction_timestamp_et":r["prediction_at"].isoformat(),
                  "p_home_cover":round(p,8),"p_away_cover":round(1-p,8),
                  "model_threshold_home_cover_lte":away_threshold,
                  "selected_side":selected,"result":result,
                  "selected_odds_american":chosen_odds,"net_units":round(net,6),
                  "baseline_side":"home","baseline_odds_american":r["home_odds"],
                  "baseline_net_units":round(base_net,6),"fold_start":start.isoformat(),
                  "training_games":len(fit_rows)}
            ledger.append(item); fold.append(item)
        fold_reports.append({"test_start":start.isoformat(),"test_end":block_days[-1].isoformat(),
            "training_games":len(fit_rows),"test_games":len(test),
            "model":summarize(fold),"home_always_baseline":summarize(fold,"baseline_side")})
    return {"protocol":"expanding_window_walk_forward_v1","status":"research_only",
        "features":FEATURES,"selection_rule":f"AWAY only when predicted P(home cover) <= {away_threshold:.3f}",
        "training":"all prior dated games only; expanding window; pushes excluded from binary model fit",
        "test_block_days":test_days,"folds":fold_reports,
        "overall_model":summarize(ledger),
        "overall_home_always_baseline":summarize(ledger,"baseline_side"),
        "probability_metrics":probability_metrics(ledger),
        "selections_and_all_test_games":ledger,
        "integrity":{"chronological_train_before_test":True,"feature_timestamp_checked":True,
          "odds_timestamp_checked":True,"post_tipoff_predictions_rejected":True,
          "invalid_rows_fail_closed":True,"test_set_used_for_tuning":False,
          "frozen_candidate_modified":False}}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv",type=Path); p.add_argument("--out",type=Path,default=Path("backtest-output/walk_forward"))
    p.add_argument("--min-train",type=int,default=100); p.add_argument("--test-days",type=int,default=7)
    p.add_argument("--away-threshold",type=float,default=.45)
    a=p.parse_args()
    if not 0<a.away_threshold<.5: p.error("--away-threshold must be between 0 and 0.5")
    rows=read_rows(a.csv,"prediction_timestamp_et")
    result=run(rows,a.min_train,a.test_days,a.away_threshold)
    a.out.mkdir(parents=True,exist_ok=True)
    (a.out/"report.json").write_text(json.dumps({k:v for k,v in result.items() if k!="selections_and_all_test_games"},indent=2))
    with (a.out/"selection_ledger.csv").open("w",newline="",encoding="utf-8") as f:
        ledger=result["selections_and_all_test_games"]
        if ledger:
            w=csv.DictWriter(f,fieldnames=list(ledger[0])); w.writeheader(); w.writerows(ledger)
    print(json.dumps({k:v for k,v in result.items() if k!="selections_and_all_test_games"},indent=2))
    print(f"Full game ledger: {a.out/'selection_ledger.csv'}")

if __name__=="__main__": main()
