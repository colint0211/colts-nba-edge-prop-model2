"""Quantitative NBA matchup projection primitives.

Sportsbook-independent scoring projection with explicit provisional uncertainty.
Residual scales are historical walk-forward estimates, not proof of calibrated
probabilities or a validated betting edge.
"""
from __future__ import annotations
import math


def _finite(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _mean(rows, key):
    values = [_finite(row[key], f"team.{key}") for row in rows if row.get(key) is not None]
    if not values:
        raise ValueError(f"No usable league {key} values.")
    return sum(values) / len(values)


def project_matchup(*, home, away, all_teams, home_adjustment=0.0,
                    away_adjustment=0.0, data_quality="unknown",
                    home_court_points=2.0, margin_residual_sd=14.81,
                    total_residual_sd=19.06):
    """Project scores from opponent-adjusted efficiency and blended pace.

    Expected offensive efficiency = league average + 60% own offensive
    deviation + 40% opponent defensive deviation. Higher opponent defensive
    rating means a weaker defense and increases the offense projection.
    Matchup pace is 75% team-average pace and 25% league pace.

    eFG, turnover, offensive-rebound and true-shooting rates are diagnostics
    only; they are already reflected in points-per-possession efficiency and
    must not be double-counted as separate scoring bonuses.
    """
    if not isinstance(home, dict) or not isinstance(away, dict):
        raise ValueError("Home and away ratings are required.")
    if not isinstance(all_teams, dict) or len(all_teams) < 2:
        raise ValueError("A league team-rating snapshot is required.")
    rows = list(all_teams.values())
    league_ortg = _mean(rows, "ortg")
    league_drtg = _mean(rows, "drtg")
    league_pace = _mean(rows, "pace")
    h_ortg = _finite(home.get("ortg"), "home.ortg")
    h_drtg = _finite(home.get("drtg"), "home.drtg")
    h_pace = _finite(home.get("pace"), "home.pace")
    a_ortg = _finite(away.get("ortg"), "away.ortg")
    a_drtg = _finite(away.get("drtg"), "away.drtg")
    a_pace = _finite(away.get("pace"), "away.pace")
    if min(h_pace, a_pace, league_pace) <= 0:
        raise ValueError("Pace values must be positive.")

    pace = 0.75 * ((h_pace + a_pace) / 2.0) + 0.25 * league_pace
    home_eff = league_ortg + 0.60 * (h_ortg - league_ortg) + 0.40 * (a_drtg - league_drtg)
    away_eff = league_ortg + 0.60 * (a_ortg - league_ortg) + 0.40 * (h_drtg - league_drtg)
    home_points = home_eff * pace / 100.0 + _finite(home_court_points, "home_court_points")
    away_points = away_eff * pace / 100.0
    home_points += _finite(home_adjustment, "home_adjustment")
    away_points += _finite(away_adjustment, "away_adjustment")

    min_games = min(int(_finite(home.get("games", 0), "home.games")),
                    int(_finite(away.get("games", 0), "away.games")))
    sample_inflation = 1.0 + 0.12 * max(0.0, (12.0 - min_games) / 12.0)
    quality_inflation = 1.08 if data_quality == "derived_early_season" else 1.0
    uncertainty_inflation = min(1.25, sample_inflation * quality_inflation)
    margin_sd = _finite(margin_residual_sd, "margin_residual_sd") * uncertainty_inflation
    total_sd = _finite(total_residual_sd, "total_residual_sd") * uncertainty_inflation
    if margin_sd <= 0 or total_sd <= 0:
        raise ValueError("Residual standard deviations must be positive.")

    score_variance = (margin_sd ** 2 + total_sd ** 2) / 4.0
    score_covariance = (total_sd ** 2 - margin_sd ** 2) / 4.0
    score_correlation = score_covariance / score_variance if score_variance else 0.0
    score_sd = math.sqrt(score_variance)
    z80 = 1.281551565545
    margin = home_points - away_points
    total = home_points + away_points
    logistic_scale = margin_sd * math.sqrt(3.0) / math.pi
    return {
        "projected_home": round(home_points, 2),
        "projected_away": round(away_points, 2),
        "projected_margin": round(margin, 2),
        "projected_total": round(total, 2),
        "projected_pace": round(pace, 2),
        "home_expected_offensive_rating": round(home_eff, 3),
        "away_expected_offensive_rating": round(away_eff, 3),
        "league_baseline": {"offensive_rating": round(league_ortg, 3),
                            "defensive_rating": round(league_drtg, 3),
                            "pace": round(league_pace, 3)},
        "factor_diagnostics": {
            "home_efg": home.get("efg"), "away_efg": away.get("efg"),
            "home_turnover_rate": home.get("tov"), "away_turnover_rate": away.get("tov"),
            "home_offensive_rebound_rate": home.get("oreb"),
            "away_offensive_rebound_rate": away.get("oreb"),
            "note": "Diagnostics only; already reflected in team efficiency and not added twice."
        },
        "uncertainty_inflation": round(uncertainty_inflation, 4),
        "score_distribution": {
            "family": "joint normal team-score approximation",
            "home_score_sd": round(score_sd, 2), "away_score_sd": round(score_sd, 2),
            "home_away_score_covariance": round(score_covariance, 3),
            "home_away_score_correlation": round(score_correlation, 4),
            "margin_sd": round(margin_sd, 3), "total_sd": round(total_sd, 3),
            "moneyline_logistic_scale": round(logistic_scale, 3),
            "home_score_80": [round(home_points-z80*score_sd, 1), round(home_points+z80*score_sd, 1)],
            "away_score_80": [round(away_points-z80*score_sd, 1), round(away_points+z80*score_sd, 1)],
            "margin_80": [round(margin-z80*margin_sd, 1), round(margin+z80*margin_sd, 1)],
            "total_80": [round(total-z80*total_sd, 1), round(total+z80*total_sd, 1)],
            "calibration_status": "provisional_walk_forward_residual_scales; probability calibration not yet validated"
        },
        "model_stage": "quantitative_matchup_engine_v1",
        "probability_calibration_status": "provisional_not_validated",
        "model_influence": "team_efficiency_matchup_projection"
    }
