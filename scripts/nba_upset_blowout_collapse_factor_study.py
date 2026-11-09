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

def sigmoid(z):
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))

def fit_logistic(train, feature_names, target_name, ridge=2.0, steps=500, lr=0.035):
    # Standardized, deterministic batch-gradient logistic regression with L2
    # regularization. No third-party packages or postgame predictors.
    columns = {}
    for name in feature_names:
        vals = [f(r, name) for r in train]
        good = [v for v in vals if v is not None]
        med = sorted(good)[len(good)//2] if good else 0.0
        vals = [med if v is None else v for v in vals]
        mu = mean(vals) or 0.0
        scale = sd(vals) or 1.0
        if scale < 1e-8: scale = 1.0
        columns[name] = (mu, scale, med)
    X = []
    y = [1.0 if r["_labels"].get(target_name) else 0.0 for r in train]
    for r in train:
        X.append([1.0] + [(columns[n][2] if f(r,n) is None else f(r,n) - columns[n][0]) / columns[n][1] for n in feature_names])
    beta = [0.0] * (len(feature_names)+1)
    n = max(1,len(train))
    for _ in range(steps):
        grad = [0.0] * len(beta)
        for xi, yi in zip(X,y):
            p = sigmoid(sum(a*b for a,b in zip(beta,xi)))
            err = p-yi
            for j,xij in enumerate(xi): grad[j] += err*xij
        for j in range(1,len(beta)):
            grad[j] += ridge*beta[j]
        beta = [b-lr*g/n for b,g in zip(beta,grad)]
    return beta, columns

def predict_logistic(rows, feature_names, beta, columns):
    out=[]
    for r in rows:
        x=[1.0]
        for name in feature_names:
            mu,scale,med=columns[name]
            value=f(r,name)
            x.append(((med if value is None else value)-mu)/scale)
        out.append(sigmoid(sum(a*b for a,b in zip(beta,x))))
    return out

def auc_score(y, probs):
    pos=sorted(p for p,t in zip(probs,y) if t==1)
    neg=sorted(p for p,t in zip(probs,y) if t==0)
    if not pos or not neg: return None
    # Mann-Whitney rank statistic with average ranks for ties.
    vals=sorted([(p,1) for p in pos]+[(p,0) for p in neg])
    rank_sum=0.0; i=0
    while i<len(vals):
        j=i+1
        while j<len(vals) and vals[j][0]==vals[i][0]: j+=1
        avg=(i+1+j)/2
        rank_sum += avg*sum(t for _,t in vals[i:j])
        i=j
    return (rank_sum-len(pos)*(len(pos)+1)/2)/(len(pos)*len(neg))

def score_predictions(rows, target, probs):
    y=[1 if r["_labels"].get(target) else 0 for r in rows]
    eps=1e-9
    brier=mean([(p-t)**2 for p,t in zip(probs,y)])
    ll=mean([-t*math.log(max(eps,p))-(1-t)*math.log(max(eps,1-p)) for p,t in zip(probs,y)])
    return {"n":len(y),"events":sum(y),"event_rate_pct":round(100*mean(y),2),
      "brier":round(brier,5),"log_loss":round(ll,5),"auc":round(auc_score(y,probs),4) if auc_score(y,probs) is not None else None}

def predictive_validation(rows):
    market=["home_spread","closing_total"]
    enhanced=market+["home_team_ortg","home_team_drtg","away_team_ortg","away_team_drtg",
      "home_team_pace","away_team_pace","home_rest_days","away_rest_days"]
    report={"design":"Two-way season holdout. Train on one season, score the other. Compare market-only closing spread/total with market plus pregame team ratings, pace and rest. Lower Brier/log-loss and higher AUC are better; this is exploratory, not proof of betting ROI.",
      "events":{}}
    for target in ["any_20pt_blowout","favorite_20pt_blowout","underdog_20pt_blowout","road_underdog_upset","high_total_bilateral_under100"]:
        report["events"][target]={}
        for train_season,test_season in [("2024-25","2025-26"),("2025-26","2024-25")]:
            train=[r for r in rows if r["_season"]==train_season]
            test=[r for r in rows if r["_season"]==test_season]
            positives=sum(bool(r["_labels"].get(target)) for r in train)
            if positives<30:
                report["events"][target][train_season+"->"+test_season]={"status":"skipped: fewer than 30 training events","training_events":positives}
                continue
            models={}
            for name,features in [("market_only",market),("market_plus_fundamentals",enhanced)]:
                beta,cols=fit_logistic(train,features,target)
                probs=predict_logistic(test,features,beta,cols)
                models[name]=score_predictions(test,target,probs)
            report["events"][target][train_season+"->"+test_season]={"training_games":len(train),"training_events":positives,"test":models}
    return report

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
    results["predictive_validation"] = predictive_validation(rows)
    with (OUT/"event_games.csv").open("w",newline="") as fp:
        fields=list(event_rows[0]) if event_rows else ["event","date","matchup"]
        w=csv.DictWriter(fp,fieldnames=fields);w.writeheader();w.writerows(event_rows)
    (OUT/"summary.json").write_text(json.dumps(results,indent=2))
    print(json.dumps(results,indent=2))
if __name__=="__main__": main()
