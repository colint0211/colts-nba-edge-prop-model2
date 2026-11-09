"""Research-only reverse engineering of NBA score formulas.

Fits ridge-regression coefficients on 2024-25 and 2025-26 pregame features,
then freezes them and evaluates on 2026-27. Does not touch live picks.
"""
from __future__ import annotations
import json, math
from collections import defaultdict, deque
from pathlib import Path

from src.api.nba_historical_backtest import (
    _download, _games, _team_rates, _rest_days, _xfeatures, _ridge,
    FEATURES,
)

SEASONS = (2025, 2026, 2027)
OUT = Path("backtest-output/reverse_engineering.json")
RIDGE_PENALTY = 2.0


def season_rows(season):
    path = _download(season)
    try:
        return _games(path)
    finally:
        Path(path).unlink(missing_ok=True)


def make_examples(rows):
    histories = defaultdict(lambda: deque(maxlen=10))
    league_games = []
    by_date = defaultdict(list)
    for row in rows:
        day, gid, home, away, hs, vs, hp, ap = row
        if hp is None or ap is None or float(hp) <= 0 or float(ap) <= 0:
            continue
        by_date[str(day)].append((str(gid), str(home), str(away),
            float(hs), float(vs), float(hp), float(ap)))

    examples = []
    for day in sorted(by_date):
        prior = league_games[-200:]
        if prior:
            poss = sum(g["poss"] + g["opp_poss"] for g in prior)
            lppp = sum(g["pf"] + g["pa"] for g in prior) / poss if poss else 1.12
            lpace = sum(g["pace"] for g in prior) / len(prior)
        else:
            lppp, lpace = 1.12, 99.0

        day_games = by_date[day]
        for gid, home, away, hs, vs, hp, ap in day_games:
            h = _team_rates(histories[home], lppp, lpace)
            a = _team_rates(histories[away], lppp, lpace)
            if h is None or a is None:
                continue
            hr = _rest_days(histories[home], day)
            ar = _rest_days(histories[away], day)
            x = _xfeatures(h, a, lppp, lpace, hr, ar)
            hoff, hdef, hpace = h
            aoff, adef, apace = a
            pace = (hpace + apace) / 2.0
            # Existing hand-built baseline, independently recorded.
            heuristic_margin = ((hoff + adef) / 2.0) * pace + 1.0 - (((aoff + hdef) / 2.0) * pace - 1.0)
            heuristic_total = ((hoff + adef) / 2.0) * pace + 1.0 + (((aoff + hdef) / 2.0) * pace - 1.0)
            examples.append({
                "date": day, "home_team": home, "away_team": away, "features": x,
                "actual_margin": hs - vs, "actual_total": hs + vs,
                "heuristic_margin": heuristic_margin,
                "heuristic_total": heuristic_total,
                "league_total_baseline": lppp * lpace * 2.0,
            })

        # Add same-date results only after all forecasts for that date.
        for gid, home, away, hs, vs, hp, ap in day_games:
            pace = (hp + ap) / 2.0
            league_games.append({"pf": hs, "pa": vs, "poss": hp,
                "opp_poss": ap, "pace": pace})
            histories[home].append({"pf": hs, "pa": vs, "poss": hp,
                "opp_poss": ap, "pace": pace, "date": day})
            histories[away].append({"pf": vs, "pa": hs, "poss": ap,
                "opp_poss": hp, "pace": pace, "date": day})
    return examples


def fit(examples, target):
    n = len(FEATURES)
    xtx = [[0.0] * n for _ in range(n)]
    xty = [0.0] * n
    for r in examples:
        x = r["features"]
        y = r[target]
        for i in range(n):
            xty[i] += x[i] * y
            for j in range(n):
                xtx[i][j] += x[i] * x[j]
    return _ridge(xtx, xty, RIDGE_PENALTY)


def metrics(rows, pred_key, actual_key):
    errors = [r[pred_key] - r[actual_key] for r in rows]
    n = len(errors)
    if not n:
        return {"games": 0}
    result = {
        "games": n,
        "mae": round(sum(abs(e) for e in errors) / n, 3),
        "rmse": round(math.sqrt(sum(e * e for e in errors) / n), 3),
        "bias": round(sum(errors) / n, 3),
    }
    if actual_key == "actual_margin":
        wins = sum((r[pred_key] > 0) == (r[actual_key] > 0) for r in rows)
        result.update({
            "outright_wins": wins,
            "outright_losses": n - wins,
            "outright_win_pct": round(100.0 * wins / n, 2),
            "record_definition": "Predicted game winner from positive/negative projected margin; not against the spread."
        })
    return result


def main():
    train = make_examples(season_rows(2025)) + make_examples(season_rows(2026))
    test = make_examples(season_rows(2027))
    if len(train) < 500 or len(test) < 30:
        raise RuntimeError(f"Insufficient data: train={len(train)}, holdout={len(test)}")

    margin_beta = fit(train, "actual_margin")
    total_beta = fit(train, "actual_total")

    for r in test:
        r["learned_margin"] = sum(a * b for a, b in zip(r["features"], margin_beta))
        r["learned_total"] = sum(a * b for a, b in zip(r["features"], total_beta))

    result = {
        "experiment": "reverse_engineered_ridge_formula_v1",
        "training_seasons": ["2024-25", "2025-26"],
        "untouched_holdout_season": "2026-27",
        "training_games": len(train),
        "holdout_games": len(test),
        "feature_names": FEATURES,
        "ridge_penalty": RIDGE_PENALTY,
        "learned_coefficients": {
            "margin": {k: round(v, 8) for k, v in zip(FEATURES, margin_beta)},
            "total": {k: round(v, 8) for k, v in zip(FEATURES, total_beta)},
        },
        "holdout_metrics": {
            "margin": {
                "learned_formula": metrics(test, "learned_margin", "actual_margin"),
                "existing_heuristic": metrics(test, "heuristic_margin", "actual_margin"),
                "home_advantage_only": metrics(
                    [dict(r, baseline=2.0) for r in test], "baseline", "actual_margin"),
            },
            "total": {
                "learned_formula": metrics(test, "learned_total", "actual_total"),
                "existing_heuristic": metrics(test, "heuristic_total", "actual_total"),
                "league_scoring_baseline": metrics(
                    [dict(r, baseline=r["league_total_baseline"]) for r in test],
                    "baseline", "actual_total"),
            },
        },
        "integrity": {
            "same_day_result_leakage": False,
            "holdout_coefficients_frozen": True,
            "holdout_season_used_for_fitting": False,
            "live_model_modified": False,
            "betting_roi_claimed": False,
        },
        "limitations": [
            "This tests score prediction, not spread/total betting profitability.",
            "No historical decision-time sportsbook prices are used.",
            "The 2026-27 holdout is still in progress; sample size is reported explicitly.",
            "Ridge regression is the first discovery method, not a claim of a globally optimal formula.",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
