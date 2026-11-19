"""Benter-style two-stage probability engine for NBA player props.

Research-only mathematical core. It does not fetch data, write to Neon, create
picks, or alter the production prediction path.

Stage 1: fit a fundamental logistic model on pregame basketball features.
Stage 2: fit a second logistic model on *out-of-sample* stage-1 probabilities
         and no-vig sportsbook probabilities, following Benter's logit
         combination idea.

The walk-forward routine is date-grouped: no game on a date can train a model
that predicts another game on that same date.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np


EPSILON = 1e-6
MODEL_VERSION = "benter-nba-two-stage-v1"


def _probability(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Probability must be finite.")
    return min(1.0 - EPSILON, max(EPSILON, value))


def _logit(value: float) -> float:
    p = _probability(value)
    return math.log(p / (1.0 - p))


def _sigmoid(value: np.ndarray | float) -> np.ndarray | float:
    x = np.clip(value, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-x))


def american_implied_probability(odds: int | float) -> float:
    """Convert American odds to raw implied probability."""
    price = float(odds)
    if not math.isfinite(price) or price == 0:
        raise ValueError("American odds must be finite and non-zero.")
    return 100.0 / (price + 100.0) if price > 0 else abs(price) / (abs(price) + 100.0)


def no_vig_probabilities(over_odds: int | float, under_odds: int | float) -> dict[str, float]:
    """Proportional no-vig probabilities for both sides of the exact same line."""
    over_raw = american_implied_probability(over_odds)
    under_raw = american_implied_probability(under_odds)
    total = over_raw + under_raw
    if total <= 0:
        raise ValueError("Invalid two-sided market prices.")
    return {"over": over_raw / total, "under": under_raw / total,
            "over_raw": over_raw, "under_raw": under_raw, "hold": total - 1.0}


def expected_value_units(win_probability: float, push_probability: float,
                         american_odds: int | float) -> float:
    """Expected net profit per one-unit stake, allowing for pushes."""
    p_win = min(1.0, max(0.0, float(win_probability)))
    p_push = min(1.0 - p_win, max(0.0, float(push_probability)))
    p_loss = 1.0 - p_win - p_push
    price = float(american_odds)
    if price == 0 or not math.isfinite(price):
        raise ValueError("American odds must be finite and non-zero.")
    net_win = price / 100.0 if price > 0 else 100.0 / abs(price)
    return p_win * net_win - p_loss


def raw_kelly_fraction(win_probability: float, push_probability: float,
                       american_odds: int | float) -> float:
    """Full Kelly fraction before any conservative fractional-Kelly haircut."""
    p_win = min(1.0, max(0.0, float(win_probability)))
    p_push = min(1.0 - p_win, max(0.0, float(push_probability)))
    p_loss = 1.0 - p_win - p_push
    price = float(american_odds)
    if price == 0 or not math.isfinite(price):
        raise ValueError("American odds must be finite and non-zero.")
    b = price / 100.0 if price > 0 else 100.0 / abs(price)
    denominator = b * (p_win + p_loss)
    if denominator <= 0:
        return 0.0
    return max(0.0, (b * p_win - p_loss) / denominator)


def fit_logistic_regression(features: Sequence[Sequence[float]],
                            targets: Sequence[int | float],
                            l2: float = 1.0,
                            max_iter: int = 100,
                            tolerance: float = 1e-8) -> dict[str, Any]:
    """Fit a standardized ridge-logistic model by Newton/IRLS.

    The intercept is not penalized. Means/scales are learned on the training
    partition only and returned for use on later observations.
    """
    x = np.asarray(features, dtype=float)
    y = np.asarray(targets, dtype=float).reshape(-1)
    if x.ndim != 2 or len(x) != len(y) or len(y) == 0:
        raise ValueError("Features must be a non-empty 2D matrix aligned to targets.")
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("Training data contains non-finite values.")
    if np.any((y < 0) | (y > 1)):
        raise ValueError("Targets must be binary values (0 or 1).")
    if len(np.unique(y)) < 2:
        raise ValueError("Both outcome classes are required to fit logistic regression.")
    if l2 < 0:
        raise ValueError("l2 must be non-negative.")

    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales < 1e-9] = 1.0
    z = (x - means) / scales
    design = np.column_stack((np.ones(len(z)), z))
    beta = np.zeros(design.shape[1], dtype=float)
    penalty = np.eye(design.shape[1], dtype=float) * float(l2)
    penalty[0, 0] = 0.0

    for _ in range(max_iter):
        p = np.clip(_sigmoid(design @ beta), EPSILON, 1.0 - EPSILON)
        gradient = design.T @ (y - p) - penalty @ beta
        weights = p * (1.0 - p)
        hessian = (design.T * weights) @ design + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.pinv(hessian) @ gradient
        beta_next = beta + step
        if np.max(np.abs(beta_next - beta)) < tolerance:
            beta = beta_next
            break
        beta = beta_next

    return {
        "intercept": float(beta[0]),
        "coefficients": beta[1:].astype(float).tolist(),
        "means": means.astype(float).tolist(),
        "scales": scales.astype(float).tolist(),
        "n": int(len(y)),
        "positive_rate": float(y.mean()),
        "l2": float(l2),
        "model_version": MODEL_VERSION,
    }


def predict_logistic(model: dict[str, Any],
                     features: Sequence[Sequence[float]]) -> list[float]:
    x = np.asarray(features, dtype=float)
    if x.ndim == 1:
        x = x.reshape(1, -1)
    means = np.asarray(model["means"], dtype=float)
    scales = np.asarray(model["scales"], dtype=float)
    coefficients = np.asarray(model["coefficients"], dtype=float)
    if x.ndim != 2 or x.shape[1] != len(means):
        raise ValueError("Prediction feature count does not match fitted model.")
    if not np.all(np.isfinite(x)):
        raise ValueError("Prediction features contain non-finite values.")
    z = (x - means) / scales
    p = _sigmoid(float(model["intercept"]) + z @ coefficients)
    return np.asarray(p, dtype=float).reshape(-1).tolist()


def fit_benter_combiner(fundamental_probabilities: Sequence[float],
                        market_probabilities: Sequence[float],
                        targets: Sequence[int | float],
                        l2: float = 0.25) -> dict[str, Any]:
    """Fit Benter's second-stage logit blend on OOS fundamental probabilities.

    For a two-outcome prop, the multinomial log-probability combination reduces
    to a logistic regression on the fundamental and market log-odds.
    """
    if not (len(fundamental_probabilities) == len(market_probabilities) == len(targets)):
        raise ValueError("Fundamental, market and target arrays must have equal length.")
    x = [[_logit(f), _logit(m)] for f, m in zip(
        fundamental_probabilities, market_probabilities
    )]
    model = fit_logistic_regression(x, targets, l2=l2)
    model["stage"] = "benter_logit_combiner"
    model["input_order"] = ["fundamental_logit", "market_logit"]
    return model


def combine_benter_probability(combiner: dict[str, Any],
                               fundamental_probability: float,
                               market_probability: float) -> float:
    row = [[_logit(fundamental_probability), _logit(market_probability)]]
    return predict_logistic(combiner, row)[0]


def walk_forward_benter(rows: Sequence[dict[str, Any]],
                        minimum_stage1_rows: int = 80,
                        minimum_stage2_rows: int = 80,
                        stage1_l2: float = 2.0,
                        stage2_l2: float = 0.25) -> dict[str, Any]:
    """Generate leakage-safe daily OOS probabilities using both Benter stages.

    Each input row must contain:
      date: ISO date (YYYY-MM-DD)
      features: numeric pregame feature vector, frozen before tipoff
      market_probability: no-vig probability for the same side and exact line
      target: 1 if that side won, otherwise 0 (pushes excluded)

    The historical stage-1 probability used to train stage 2 is itself
    generated out-of-sample on that row's date. Predictions are emitted only
    after both stages have enough prior observations; there is no silent
    fallback to an unvalidated fixed blend.
    """
    clean = []
    for index, row in enumerate(rows):
        date = str(row.get("date") or "")
        if len(date) < 10:
            raise ValueError(f"Row {index} has no ISO calendar date.")
        features = [float(v) for v in row["features"]]
        market = _probability(float(row["market_probability"]))
        target = int(row["target"])
        if target not in (0, 1):
            raise ValueError("Pushes must be excluded; target must be 0 or 1.")
        if not features or not all(math.isfinite(v) for v in features):
            raise ValueError(f"Row {index} has invalid pregame features.")
        clean.append({**row, "date": date[:10], "features": features,
                      "market_probability": market, "target": target,
                      "_index": index})

    if not clean:
        return {"status": "insufficient_data", "predictions": [],
                "reason": "No eligible settled pregame observations."}
    feature_counts = {len(r["features"]) for r in clean}
    if len(feature_counts) != 1:
        raise ValueError("All feature vectors must have the same length.")

    dates = sorted({r["date"] for r in clean})
    oos_fundamental: dict[int, float] = {}
    output = []
    diagnostics = []

    for current_date in dates:
        prior = [r for r in clean if r["date"] < current_date]
        current = [r for r in clean if r["date"] == current_date]
        if len(prior) < minimum_stage1_rows:
            diagnostics.append({"date": current_date, "status": "waiting_for_stage1_history",
                                "prior_rows": len(prior)})
            continue

        stage1 = fit_logistic_regression(
            [r["features"] for r in prior], [r["target"] for r in prior],
            l2=stage1_l2
        )
        current_fundamental = predict_logistic(
            stage1, [r["features"] for r in current]
        )

        # Stage 2 sees only prior dates' stored OOS fundamental estimates.
        combiner_rows = [r for r in prior if r["_index"] in oos_fundamental]
        if len(combiner_rows) < minimum_stage2_rows:
            for r, p_fund in zip(current, current_fundamental):
                oos_fundamental[r["_index"]] = p_fund
            diagnostics.append({"date": current_date, "status": "waiting_for_stage2_history",
                                "stage1_prior_rows": len(prior),
                                "stage2_oos_rows": len(combiner_rows)})
            continue

        combiner = fit_benter_combiner(
            [oos_fundamental[r["_index"]] for r in combiner_rows],
            [r["market_probability"] for r in combiner_rows],
            [r["target"] for r in combiner_rows],
            l2=stage2_l2
        )
        for r, p_fund in zip(current, current_fundamental):
            p_combined = combine_benter_probability(
                combiner, p_fund, r["market_probability"]
            )
            output.append({
                "input_index": r["_index"], "date": current_date,
                "fundamental_probability": p_fund,
                "market_probability": r["market_probability"],
                "combined_probability": p_combined,
                "target": r["target"],
                "model_version": MODEL_VERSION,
                "stage1_training_rows": len(prior),
                "stage2_training_oos_rows": len(combiner_rows),
            })
            oos_fundamental[r["_index"]] = p_fund

        diagnostics.append({"date": current_date, "status": "predicted",
                            "stage1_prior_rows": len(prior),
                            "stage2_oos_rows": len(combiner_rows)})

    return {
        "status": "complete" if output else "insufficient_data",
        "model_version": MODEL_VERSION,
        "method": "Expanding-window daily walk-forward; stage 1 fundamental logistic model; stage 2 Benter logit combination of stage-1 OOS and no-vig market probabilities.",
        "eligible_rows": len(clean),
        "first_date": dates[0],
        "last_date": dates[-1],
        "minimum_stage1_rows": minimum_stage1_rows,
        "minimum_stage2_rows": minimum_stage2_rows,
        "predictions": output,
        "diagnostics": diagnostics,
        "note": "Features must be timestamped/frozen pregame. The routine does not impute missing features or create same-day training leakage.",
    }


def probability_metrics(predictions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(predictions)
    if not rows:
        return {"n": 0, "brier": None, "log_loss": None, "accuracy_50": None}
    brier = 0.0
    log_loss = 0.0
    correct = 0
    for row in rows:
        p = _probability(row["combined_probability"])
        y = int(row["target"])
        brier += (p - y) ** 2
        log_loss -= math.log(p if y else 1.0 - p)
        correct += int((p >= 0.5) == bool(y))
    n = len(rows)
    return {"n": n, "brier": brier / n, "log_loss": log_loss / n,
            "accuracy_50": correct / n}
