"""Train on 2024-25 and evaluate 2025-26; close-snapshot picks are diagnostic only, not executable bets."""
from __future__ import annotations
import json, math, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.nba_historical_matchup_backtest import (
    load_team_games, build_ratings, rows, team_code, project_matchup,
    logistic_cdf, normal_cdf, normal_market_probs, ev_at_minus_110, grade, summarize,
    LINES_URL, OUT
)

def ols3(records, target, x1, x2):
    # Solve (X'X)b=X'y by pivoted Gauss-Jordan elimination.
    a=[[0.0]*4 for _ in range(3)]
    for r in records:
        x=[1.0,float(r[x1]),float(r[x2])]
        y=float(r[target])
        for i in range(3):
            for j in range(3): a[i][j]+=x[i]*x[j]
            a[i][3]+=x[i]*y
    for col in range(3):
        pivot=max(range(col,3),key=lambda row:abs(a[row][col]))
        if abs(a[pivot][col])<1e-10: raise ValueError("Singular calibration matrix")
        a[col],a[pivot]=a[pivot],a[col]
        d=a[col][col]
        a[col]=[v/d for v in a[col]]
        for row in range(3):
            if row==col: continue
            k=a[row][col]
            a[row]=[a[row][j]-k*a[col][j] for j in range(4)]
    return [a[i][3] for i in range(3)]

def sd(values):
    m=sum(values)/len(values)
    return math.sqrt(sum((x-m)**2 for x in values)/(len(values)-3))

def rmse(records, pred, target):
    return math.sqrt(sum((r[target]-pred(r))**2 for r in records)/len(records))

def collect(season):
    box,nrows=load_team_games(season)
    su=season.replace("-","_")
    lr=rows(LINES_URL.format(season=season,season_underscore=su))
    lr=[r for r in lr if str(r.get("season_type","")).lower()=="regular"]
    days=defaultdict(list)
    for r in lr:
        h=team_code(r.get("home_team")); a=team_code(r.get("away_team"))
        if not h or not a: continue
        d=str(r.get("date_et",""))[:10]
        days[d].append({"date":d,"home":h,"away":a,
          "home_score":float(r["home_score"]),"away_score":float(r["away_score"]),
          "close_spread_home":float(r["close_spread_home"]),"close_total":float(r["close_total"]),
          "close_home_cover_prob":float(r["close_home_cover_prob"]),
          "close_over_prob":float(r["close_over_prob"])})
    hist=defaultdict(list); out=[]
    for day in sorted(days):
        ratings=build_ratings(hist)
        for g in days[day]:
            h=ratings.get(g["home"]); a=ratings.get(g["away"])
            if not h or not a: continue
            try:
                m=project_matchup(home=h,away=a,all_teams=ratings,data_quality="derived_early_season",
                    home_court_points=2.0,margin_residual_sd=14.81,total_residual_sd=19.06)
            except Exception: continue
            g.update({"raw_margin":m["projected_margin"],"raw_total":m["projected_total"]})
            g["actual_margin"]=g["home_score"]-g["away_score"]
            g["actual_total"]=g["home_score"]+g["away_score"]
            g["market_margin"]=-g["close_spread_home"]
            out.append(g)
        for tg in box.get(day,[]): hist[tg["team"]].append(tg)
    return out

def apply_calibration(records, bm, bt, margin_sd, total_sd, season):
    byday=defaultdict(lambda:{"sides":[],"totals":[]})
    allrows=[]
    for g in records:
        margin=bm[0]+bm[1]*g["raw_margin"]+bm[2]*g["market_margin"]
        total=bt[0]+bt[1]*g["raw_total"]+bt[2]*g["close_total"]
        row=dict(g,cal_margin=margin,cal_total=total)
        allrows.append(row)
        # Probability distribution calibrated to residuals in training season.
        scale=margin_sd*math.sqrt(3)/math.pi
        home_line=g["close_spread_home"]
        home_p=1-logistic_cdf(.5 if abs(home_line-round(home_line))<1e-9 else 0,
                               margin+home_line,scale)
        home_push=(logistic_cdf(.5,margin+home_line,scale)-logistic_cdf(-.5,margin+home_line,scale)) if abs(home_line-round(home_line))<1e-9 else 0
        for team,line,p_mkt,p in (
          (g["home"],home_line,g["close_home_cover_prob"],home_p),
          (g["away"],-home_line,1-g["close_home_cover_prob"],
           1-logistic_cdf(.5 if abs(home_line-round(home_line))<1e-9 else 0,
                           -margin-home_line,scale))):
            ev=ev_at_minus_110(p,home_push)
            edge=(p-p_mkt)*100
            if edge>=4 and ev*100>=2 and p>=.5 and edge<=12 and ev*100<=20:
                byday[g["date"]]["sides"].append({"market":"sides","selection":team,"line":line,
                    "actual_margin":g["actual_margin"],"actual_total":g["actual_total"],"home":g["home"],
                    "prob":p,"market_prob":p_mkt,"edge_pp":edge,"ev_pct":ev*100,"date":g["date"]})
        line=g["close_total"]
        for side,over in (("OVER",True),("UNDER",False)):
            p,push=normal_market_probs(total,total_sd,line,over)
            p_mkt=g["close_over_prob"] if over else 1-g["close_over_prob"]
            ev=ev_at_minus_110(p,push); edge=(p-p_mkt)*100
            if edge>=4 and ev*100>=2 and p>=.5 and edge<=12 and ev*100<=20:
                byday[g["date"]]["totals"].append({"market":"totals","selection":side,"line":line,
                    "actual_margin":g["actual_margin"],"actual_total":g["actual_total"],"home":g["home"],
                    "prob":p,"market_prob":p_mkt,"edge_pp":edge,"ev_pct":ev*100,"date":g["date"]})
    picks=[]
    for d,v in byday.items():
        for market in ("sides","totals"):
            picks.extend(sorted(v[market],key=lambda x:(x["ev_pct"],x["edge_pp"]),reverse=True)[:5])
    return allrows,picks

def main():
    train=collect("2024-25")
    test=collect("2025-26")
    bm=ols3(train,"actual_margin","raw_margin","market_margin")
    bt=ols3(train,"actual_total","raw_total","close_total")
    mr=[r["actual_margin"]-(bm[0]+bm[1]*r["raw_margin"]+bm[2]*r["market_margin"]) for r in train]
    tr=[r["actual_total"]-(bt[0]+bt[1]*r["raw_total"]+bt[2]*r["close_total"]) for r in train]
    ms=sd(mr); ts=sd(tr)
    cal, picks=apply_calibration(test,bm,bt,ms,ts,"2025-26")
    report={
      "train_season":"2024-25","untouched_test_season":"2025-26",
      "coefficients":{"margin":{"intercept":bm[0],"raw_model":bm[1],"market_margin":bm[2]},
                      "total":{"intercept":bt[0],"raw_model":bt[1],"market_total":bt[2]}},
      "calibrated_residual_sd":{"margin":ms,"total":ts},
      "test_rmse":{"margin":{"raw_model":rmse(test,lambda r:r["raw_margin"],"actual_margin"),
                            "closing_market":rmse(test,lambda r:r["market_margin"],"actual_margin"),
                            "calibrated":rmse(cal,lambda r:r["cal_margin"],"actual_margin")},
                   "total":{"raw_model":rmse(test,lambda r:r["raw_total"],"actual_total"),
                            "closing_market":rmse(test,lambda r:r["close_total"],"actual_total"),
                            "calibrated":rmse(cal,lambda r:r["cal_total"],"actual_total")}},
      "hypothetical_close_snapshot_selection_diagnostic_not_bettable":{"sides":summarize([p for p in picks if p["market"]=="sides"]),
                     "totals":summarize([p for p in picks if p["market"]=="totals"]),
                     "combined":summarize(picks)},
      "test_games":len(test),"test_picks":len(picks),
      "interpretation":"Only 2024-25 fits coefficients; 2025-26 is untouched out-of-sample. The displayed selection record uses closing lines and assumed -110 prices, so it is NOT an executable betting backtest, does NOT measure CLV, and must not be presented as realized or achievable ROI. See market_validation_report.json for market-only versus market-plus-fundamental Brier/log-loss comparison."
    }
    OUT.mkdir(exist_ok=True)
    (OUT/"calibration_report.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
if __name__=="__main__":main()
