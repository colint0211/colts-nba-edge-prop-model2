"""Point-in-time, chronological NBA ATS feature study.

Input: a CSV with one row per game and the schema documented in
research/ATS_FEATURE_STUDY.md. This is a research experiment only; it does not
import or modify the frozen production/candidate ATS model.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path

FEATURES = [
    "spread_move_toward_home",
    "home_win_probability_move",
    "total_move",
    "elo_diff_home",
    "rolling_net_rating_diff",
    "rest_days_diff",
    "schedule_burden_diff",
    "injury_availability_diff",
    "rolling_off_def_rating_diff",
    "travel_tz_diff",
]
REQUIRED = [
    "date_et", "game_id", "prediction_timestamp_et", "feature_available_at_et",
    "home_score", "away_score", "spread_home", "away_odds_american", *FEATURES,
]


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load(path: Path):
    rows = []
    pushes_excluded = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            if not all(raw.get(k, "").strip() for k in REQUIRED):
                continue
            prediction_at = dt(raw["prediction_timestamp_et"])
            available_at = dt(raw["feature_available_at_et"])
            if prediction_at.tzinfo is None or available_at.tzinfo is None:
                raise ValueError("Timezone offsets are required for prediction and feature timestamps")
            if available_at > prediction_at:
                raise ValueError(
                    f"LEAKAGE: {raw['game_id']} feature timestamp "
                    f"{available_at.isoformat()} is after prediction timestamp "
                    f"{prediction_at.isoformat()}"
                )
            row = {"date": dt(raw["date_et"]), "game_id": raw["game_id"]}
            row["y"] = int(
                float(raw["home_score"]) - float(raw["away_score"])
                + float(raw["spread_home"]) > 0
            )
            row["push"] = (
                float(raw["home_score"]) - float(raw["away_score"])
                + float(raw["spread_home"]) == 0
            )
            row["odds"] = float(raw["away_odds_american"])
            if row["push"]:
                pushes_excluded += 1
                continue
            for name in FEATURES:
                row[name] = float(raw[name])
            rows.append(row)
    rows.sort(key=lambda x: (x["date"], x["game_id"]))
    if len(rows) < 100:
        raise ValueError(f"Only {len(rows)} usable rows; require at least 100.")
    if len({r["game_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate game_id rows detected.")
    return rows, pushes_excluded


def sigmoid(z):
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def fit(rows, features, ridge=1.0):
    means, scales = {}, {}
    for f in features:
        vals = [r[f] for r in rows]
        means[f] = sum(vals) / len(vals)
        sd = math.sqrt(sum((v-means[f])**2 for v in vals) / max(1, len(vals)-1))
        scales[f] = sd or 1.0
    # Newton/IRLS for logistic regression with L2 penalty; intercept unpenalized.
    p = len(features) + 1
    beta = [0.0] * p
    for _ in range(100):
        h = [[0.0] * p for _ in range(p)]
        g = [0.0] * p
        for r in rows:
            x = [1.0] + [(r[f]-means[f])/scales[f] for f in features]
            prob = sigmoid(sum(a*b for a,b in zip(x,beta)))
            w = max(1e-8, prob*(1-prob))
            for i in range(p):
                g[i] += x[i]*(r["y"]-prob)
                for j in range(p):
                    h[i][j] += w*x[i]*x[j]
        for j in range(1,p):
            h[j][j] += ridge
            g[j] -= ridge*beta[j]
        delta = solve(h,g)
        beta = [b+d for b,d in zip(beta,delta)]
        if max(abs(d) for d in delta) < 1e-7:
            break
    return {"features":features,"means":means,"scales":scales,"beta":beta}


def solve(a,b):
    m=[list(row)+[v] for row,v in zip(a,b)]
    n=len(b)
    for c in range(n):
        pivot=max(range(c,n),key=lambda r:abs(m[r][c]))
        m[c],m[pivot]=m[pivot],m[c]
        if abs(m[c][c])<1e-12:
            m[c][c]=1e-12
        z=m[c][c]
        for j in range(c,n+1): m[c][j]/=z
        for r in range(n):
            if r==c: continue
            z=m[r][c]
            for j in range(c,n+1): m[r][j]-=z*m[c][j]
    return [m[i][n] for i in range(n)]


def predict(model,row):
    x=[1.0]+[(row[f]-model["means"][f])/model["scales"][f] for f in model["features"]]
    return sigmoid(sum(a*b for a,b in zip(x,model["beta"])))


def metrics(rows, probs):
    eps=1e-12
    n=len(rows)
    brier=sum((p-r["y"])**2 for r,p in zip(rows,probs))/n
    logloss=-sum(r["y"]*math.log(max(eps,p))+(1-r["y"])*math.log(max(eps,1-p))
                 for r,p in zip(rows,probs))/n
    acc=sum((p>=.5)==bool(r["y"]) for r,p in zip(rows,probs))/n
    # Illustrative flat -110 away ATS selection; only take away when P(home cover)<=45%.
    picks=[(r,p) for r,p in zip(rows,probs) if p<=.45 and not r["push"]]
    wins=sum(1 for r,p in picks if r["y"]==0)
    losses=len(picks)-wins
    units=wins-(losses*110/100)
    return {
        "games":n,"accuracy_pct":round(100*acc,2),
        "brier":round(brier,5),"log_loss":round(logloss,5),
        "away_rule":{"bets":len(picks),"wins":wins,"losses":losses,
                     "win_pct":round(100*wins/len(picks),2) if picks else None,
                     "units_flat_minus_110":round(units,2)}
    }


def run(rows, pushes_excluded=0):
    n=len(rows)
    a=int(n*.70); b=int(n*.85)
    train,valid,test=rows[:a],rows[a:b],rows[b:]
    if min(len(train),len(valid),len(test))<10:
        raise ValueError("Insufficient rows in chronological train/validation/test split.")
    experiments={"intercept_only":[]}
    experiments.update({f:[f] for f in FEATURES})
    experiments["all_10"]=FEATURES
    results={}
    for name,features in experiments.items():
        model=fit(train,features)
        results[name]={
            "features":features,
            "validation":metrics(valid,[predict(model,r) for r in valid]),
            "untouched_test":metrics(test,[predict(model,r) for r in test]),
        }
    # Ablation: full ten-feature model minus each variable, same frozen split.
    ablation={}
    for removed in FEATURES:
        kept=[f for f in FEATURES if f!=removed]
        model=fit(train,kept)
        ablation[removed]=metrics(test,[predict(model,r) for r in test])
    return {
        "experiment":"nba_ats_ten_feature_study_v1",
        "status":"research_only_not_production",
        "rows":n,"pushes_excluded":pushes_excluded,
        "split":{"train":len(train),"validation":len(valid),"untouched_test":len(test),
                 "chronological":True},
        "metrics_note":"No tuning on untouched test. Compare Brier/log-loss to intercept-only; ROI is descriptive and assumes flat -110.",
        "experiments":results,"full_model_ablation_untouched_test":ablation,
        "integrity":{"feature_timestamps_checked":True,"future_features_rejected":True,
                     "frozen_away_only_candidate_modified":False}
    }


if __name__ == "__main__":
    if len(sys.argv)!=2:
        raise SystemExit("Usage: python scripts/nba_ats_feature_study.py path/to/point_in_time_ats_features.csv")
    rows, pushes_excluded = load(Path(sys.argv[1]))
    result=run(rows, pushes_excluded)
    out=Path("backtest-output/ats_feature_study")
    out.mkdir(parents=True,exist_ok=True)
    (out/"results.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
