"""Market-anchored, chronological probability validation for NBA matchups.

Fits only on 2024-25, evaluates only on untouched 2025-26. This evaluates
forecast quality at the closing snapshot; it is NOT a bettable backtest because
the archive does not contain timestamped prices available at wager placement.
No picks, ROI, or staking recommendations are produced here.
"""
from __future__ import annotations
import json
import math
from pathlib import Path

from nba_calibrate_matchup import collect, ols3, sd, OUT


def logistic_cdf(x: float, mean: float, scale: float) -> float:
    z = max(-35.0, min(35.0, (x - mean) / scale))
    return 1.0 / (1.0 + math.exp(-z))


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def score_metrics(rows, probability_key, outcome_key):
    usable = [r for r in rows if r.get(outcome_key) is not None and r.get(probability_key) is not None]
    if not usable:
        return {"n": 0, "brier": None, "log_loss": None, "calibration_bins": []}
    probs = [min(1 - 1e-9, max(1e-9, float(r[probability_key]))) for r in usable]
    outcomes = [float(r[outcome_key]) for r in usable]
    bins = []
    for lo in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        group = [(p, y) for p, y in zip(probs, outcomes) if lo <= p < lo + 0.1 or (lo == 0.9 and p == 1.0)]
        if group:
            bins.append({"range": f"{lo:.1f}-{lo+0.1:.1f}", "n": len(group),
                         "mean_probability": round(sum(p for p, _ in group) / len(group), 4),
                         "observed_rate": round(sum(y for _, y in group) / len(group), 4)})
    return {
        "n": len(usable),
        "brier": round(sum((p-y)**2 for p, y in zip(probs, outcomes)) / len(usable), 6),
        "log_loss": round(-sum(y*math.log(p)+(1-y)*math.log(1-p) for p, y in zip(probs, outcomes)) / len(usable), 6),
        "calibration_bins": bins,
    }


def evaluate(train, test):
    margin_beta = ols3(train, "actual_margin", "raw_margin", "market_margin")
    total_beta = ols3(train, "actual_total", "raw_total", "close_total")
    margin_resid = [r["actual_margin"] - (margin_beta[0] + margin_beta[1]*r["raw_margin"] + margin_beta[2]*r["market_margin"]) for r in train]
    total_resid = [r["actual_total"] - (total_beta[0] + total_beta[1]*r["raw_total"] + total_beta[2]*r["close_total"]) for r in train]
    margin_sigma, total_sigma = sd(margin_resid), sd(total_resid)
    results = {"sides": [], "totals": []}

    for g in test:
        margin_hat = margin_beta[0] + margin_beta[1]*g["raw_margin"] + margin_beta[2]*g["market_margin"]
        total_hat = total_beta[0] + total_beta[1]*g["raw_total"] + total_beta[2]*g["close_total"]
        row = dict(g)
        # Closing-line market probabilities are de-vigged probabilities from the source archive.
        market_home = min(1.0, max(0.0, float(g["close_home_cover_prob"])))
        market_over = min(1.0, max(0.0, float(g["close_over_prob"])))
        line = float(g["close_spread_home"])
        scale = margin_sigma * math.sqrt(3.0) / math.pi
        model_home = 1.0 - logistic_cdf(0.5 if abs(line-round(line)) < 1e-9 else 0.0,
                                         margin_hat + line, scale)
        if abs(line-round(line)) < 1e-9:
            push = logistic_cdf(0.5, margin_hat+line, scale) - logistic_cdf(-0.5, margin_hat+line, scale)
            # Score only decisive outcomes; conditional probabilities remove push mass.
            if abs(g["actual_margin"] + line) < 1e-9:
                home_outcome = None
            else:
                home_outcome = 1.0 if g["actual_margin"] + line > 0 else 0.0
            model_home = model_home / max(1e-9, 1.0-push)
            market_home = market_home
        else:
            home_outcome = 1.0 if g["actual_margin"] + line > 0 else 0.0
        if home_outcome is not None:
            results["sides"].append({"market_p": market_home, "model_p": min(1.0,max(0.0,model_home)), "y": home_outcome})

        total_line = float(g["close_total"])
        # Probability of going OVER the total; push mass excluded for integer lines.
        model_over = 1.0-normal_cdf(total_line + (0.5 if abs(total_line-round(total_line)) < 1e-9 else 0.0),
                                    total_hat, total_sigma)
        if abs(total_line-round(total_line)) < 1e-9:
            push = normal_cdf(total_line+0.5,total_hat,total_sigma)-normal_cdf(total_line-0.5,total_hat,total_sigma)
            total_outcome = None if abs(g["actual_total"]-total_line) < 1e-9 else (1.0 if g["actual_total"] > total_line else 0.0)
            model_over = model_over / max(1e-9,1.0-push)
        else:
            total_outcome = 1.0 if g["actual_total"] > total_line else 0.0
        if total_outcome is not None:
            results["totals"].append({"market_p": market_over, "model_p": min(1.0,max(0.0,model_over)), "y": total_outcome})

    report = {"train_season":"2024-25","untouched_test_season":"2025-26",
      "training_coefficients":{"margin":{"intercept":margin_beta[0],"fundamental_model":margin_beta[1],"closing_market_margin":margin_beta[2]},
                               "total":{"intercept":total_beta[0],"fundamental_model":total_beta[1],"closing_market_total":total_beta[2]}},
      "training_residual_sd":{"margin":margin_sigma,"total":total_sigma},
      "test_forecast_scores":{
        "sides":{"market_only":score_metrics(results["sides"],"market_p","y"),"market_plus_fundamental":score_metrics(results["sides"],"model_p","y")},
        "totals":{"market_only":score_metrics(results["totals"],"market_p","y"),"market_plus_fundamental":score_metrics(results["totals"],"model_p","y")}},
      "test_games":len(test),
      "interpretation":"This is an out-of-sample closing-snapshot forecast comparison only. It does not establish betting profitability, CLV, or executable ROI. The historical archive has closing lines/probabilities but no time-stamped decision-time prices. No actionable picks are generated.",
      "next_data_requirement":["timestamped DraftKings spread/total lines and American prices before tipoff","at least two or more snapshots per event to measure line movement and CLV","pregame injury/availability and lineup snapshots with publication timestamps"]}
    return report


def main():
    report = evaluate(collect("2024-25"), collect("2025-26"))
    OUT.mkdir(exist_ok=True)
    (OUT/"market_validation_report.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
