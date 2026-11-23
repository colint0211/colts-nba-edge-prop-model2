"""Totals-only NBA chronological walk-forward validation.

Replays season-to-date team offensive/defensive efficiency and pace using only
games completed before each date. Training seasons fit the additive total
calibration and residual variance; the latest season is an untouched holdout.
No sportsbook odds are present in the public source, so ROI/CLV are not claimed.
"""
import json
import math
import os
import tempfile
from collections import defaultdict, deque
from urllib.request import Request, urlopen

import duckdb

BASE = "https://github.com/ramirobentes/nba_data/releases/download/pbp"
SEASONS = (2025, 2026, 2027)
TRAIN_SEASONS = (2025, 2026)
HOLDOUT_SEASON = 2027
MIN_PRIOR_GAMES = 3
PRIOR_STRENGTH = 8.0
MIN_PRIOR_WEIGHT = 0.12
MAX_PRIOR_WEIGHT = 0.65
HOME_COURT_MARGIN = 2.0


def download(url, path):
    request = Request(url, headers={
        "User-Agent": "ColtsNBATotals/1.0",
        "Accept": "application/octet-stream",
    })
    total = 0
    with urlopen(request, timeout=45) as response, open(path, "wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 80_000_000:
                raise RuntimeError("NBA PBP parquet exceeds the 80 MB safety limit.")
            output.write(chunk)
    return total


def team_rating(rows):
    if len(rows) < MIN_PRIOR_GAMES:
        return None
    poss_for = sum(x[0] for x in rows)
    points_for = sum(x[1] for x in rows)
    poss_against = sum(x[2] for x in rows)
    points_against = sum(x[3] for x in rows)
    if min(poss_for, poss_against) <= 0:
        return None
    games = len(rows)
    return {
        "off": points_for / poss_for,
        "def": points_against / poss_against,
        "pace": (poss_for + poss_against) / (2.0 * games),
        "games": games,
    }


def league_environment(games):
    if not games:
        return {"ppp": 1.12, "pace": 99.0}
    possessions = sum(g["home_poss"] + g["away_poss"] for g in games)
    points = sum(g["home_score"] + g["away_score"] for g in games)
    return {
        "ppp": points / possessions if possessions else 1.12,
        "pace": possessions / (2.0 * len(games)),
    }


def shrink_rating(raw, league):
    weight = max(MIN_PRIOR_WEIGHT, min(MAX_PRIOR_WEIGHT,
                                       PRIOR_STRENGTH / (raw["games"] + PRIOR_STRENGTH)))
    return {
        "off": raw["off"] * (1.0 - weight) + league["ppp"] * weight,
        "def": raw["def"] * (1.0 - weight) + league["ppp"] * weight,
        "pace": raw["pace"] * (1.0 - weight) + league["pace"] * weight,
        "games": raw["games"],
        "prior_weight": weight,
    }


def predict(home, away, history, league):
    raw_home = team_rating(history[home])
    raw_away = team_rating(history[away])
    if not raw_home or not raw_away:
        return None
    h = shrink_rating(raw_home, league)
    a = shrink_rating(raw_away, league)
    pace = 0.5 * (h["pace"] + a["pace"])
    home_eff = 0.5 * (h["off"] + a["def"])
    away_eff = 0.5 * (a["off"] + h["def"])
    # Home-court advantage affects the margin, not the combined total.
    home_points = home_eff * pace + HOME_COURT_MARGIN / 2.0
    away_points = away_eff * pace - HOME_COURT_MARGIN / 2.0
    return {
        "home": home_points, "away": away_points,
        "total": home_points + away_points,
        "home_prior": h["games"], "away_prior": a["games"],
        "league_ppp": league["ppp"], "league_pace": league["pace"],
        "projected_pace": pace,
    }


def summarize(rows, offset=0.0):
    if not rows:
        return None
    residuals = [r["total"] + offset - r["actual_total"] for r in rows]
    n = len(residuals)
    bias = sum(residuals) / n
    mae = sum(abs(x) for x in residuals) / n
    rmse = math.sqrt(sum(x * x for x in residuals) / n)
    residual_sd = math.sqrt(sum((x - bias) ** 2 for x in residuals) / n)
    return {
        "games": n,
        "total_mae": round(mae, 3),
        "total_rmse": round(rmse, 3),
        "total_bias": round(bias, 3),
        "total_residual_sd": round(residual_sd, 3),
    }


def normal_cdf(value, mean, sd):
    return 0.5 * (1.0 + math.erf((value - mean) / (sd * math.sqrt(2.0))))


def probability_calibration(rows, residual_sd, offset):
    """Evaluate the fitted normal distribution on predeclared pseudo-lines.

    Historical executable sportsbook totals are unavailable. These offsets
    around each forecast are a distribution calibration diagnostic, not ROI.
    """
    offsets = (-15.5, -10.5, -5.5, -0.5, 4.5, 9.5, 14.5)
    observations = []
    for row in rows:
        mean = float(row["total"]) + float(offset)
        actual = float(row["actual_total"])
        for delta in offsets:
            line = mean + delta
            probability = 1.0 - normal_cdf(line, mean, residual_sd)
            outcome = 1.0 if actual > line else 0.0
            probability = max(1e-9, min(1.0 - 1e-9, probability))
            observations.append({
                "predicted_over_probability": probability,
                "actual_over": outcome,
                "delta_from_projection": delta,
            })
    if not observations:
        return None
    n = len(observations)
    brier = sum((x["predicted_over_probability"] - x["actual_over"]) ** 2
                for x in observations) / n
    log_loss = -sum(
        x["actual_over"] * math.log(x["predicted_over_probability"])
        + (1.0 - x["actual_over"]) * math.log(1.0 - x["predicted_over_probability"])
        for x in observations
    ) / n
    bins = []
    for lower in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        upper = lower + 0.1
        bucket = [x for x in observations
                  if lower <= x["predicted_over_probability"] < upper]
        if bucket:
            predicted = sum(x["predicted_over_probability"] for x in bucket) / len(bucket)
            observed = sum(x["actual_over"] for x in bucket) / len(bucket)
            bins.append({
                "probability_range": f"{lower:.1f}-{upper:.1f}",
                "thresholds": len(bucket),
                "predicted_over_pct": round(predicted * 100.0, 2),
                "observed_over_pct": round(observed * 100.0, 2),
                "absolute_calibration_error": round(abs(predicted - observed), 5),
            })
    weighted_error = (
        sum(x["absolute_calibration_error"] * x["thresholds"] for x in bins)
        / sum(x["thresholds"] for x in bins)
    ) if bins else None
    return {
        "thresholds_scored": n,
        "pseudo_lines_per_game": len(offsets),
        "brier": round(brier, 5),
        "log_loss": round(log_loss, 5),
        "brier_baseline_50_50": 0.25,
        "brier_skill_vs_50_50": round(1.0 - brier / 0.25, 5),
        "mean_absolute_calibration_error": round(weighted_error, 5) if weighted_error is not None else None,
        "bins": bins,
        "note": "Predeclared pseudo-lines at fixed offsets from each forecast; not historical DraftKings lines or a betting ROI test.",
    }


def load_games():
    games = []
    files = {}
    with tempfile.TemporaryDirectory(prefix="nba-totals-walkforward-") as folder:
        connection = duckdb.connect(":memory:")
        try:
            for season in SEASONS:
                path = os.path.join(folder, f"pbp_regular_{season}.parquet")
                files[str(season)] = {
                    "file": os.path.basename(path),
                    "bytes": download(f"{BASE}/pbp_regular_{season}.parquet", path),
                }
                escaped = path.replace("'", "''")
                rows = connection.execute(f"""
                  SELECT game_date,game_id,team_home,team_away,
                         MAX(hs) AS home_score,MAX(vs) AS away_score,
                         MAX(poss_home) AS home_poss,MAX(poss_away) AS away_poss
                  FROM read_parquet('{escaped}')
                  WHERE season_type='regular'
                  GROUP BY game_date,game_id,team_home,team_away
                  HAVING MAX(hs)>0 AND MAX(vs)>0
                     AND MAX(poss_home)>0 AND MAX(poss_away)>0
                  ORDER BY game_date,game_id
                """).fetchall()
                for row in rows:
                    games.append({
                        "date": str(row[0]), "season": season, "game_id": str(row[1]),
                        "home": str(row[2]), "away": str(row[3]),
                        "home_score": float(row[4]), "away_score": float(row[5]),
                        "home_poss": float(row[6]), "away_poss": float(row[7]),
                    })
        finally:
            connection.close()
    return sorted(games, key=lambda x: (x["date"], x["game_id"])), files


def run(output_path="nba_totals_backtest.json"):
    games, files = load_games()
    history = defaultdict(list)
    league_games = deque(maxlen=200)
    predictions = []
    active_season = None
    idx = 0
    while idx < len(games):
        day = games[idx]["date"]
        batch = []
        while idx < len(games) and games[idx]["date"] == day:
            batch.append(games[idx])
            idx += 1
        season = batch[0]["season"]
        if season != active_season:
            active_season = season
            history = defaultdict(list)
            league_games = deque(maxlen=200)
        league = league_environment(list(league_games))
        day_predictions = []
        for game in batch:
            forecast = predict(game["home"], game["away"], history, league)
            if forecast:
                actual = game["home_score"] + game["away_score"]
                day_predictions.append({
                    "date": day, "season": season, "game_id": game["game_id"],
                    "home": game["home"], "away": game["away"],
                    "projected_home": forecast["home"],
                    "projected_away": forecast["away"],
                    "projected_total": forecast["total"],
                    "actual_home": game["home_score"],
                    "actual_away": game["away_score"],
                    "actual_total": actual,
                    "total": forecast["total"],
                    "total_abs_error": abs(forecast["total"] - actual),
                    "home_prior": forecast["home_prior"],
                    "away_prior": forecast["away_prior"],
                    "projected_pace": forecast["projected_pace"],
                    "league_ppp_prior": forecast["league_ppp"],
                    "league_pace_prior": forecast["league_pace"],
                })
        predictions.extend(day_predictions)
        # No result from this date enters another same-day forecast.
        for game in batch:
            history[game["home"]].append((
                game["home_poss"], game["home_score"],
                game["away_poss"], game["away_score"],
            ))
            history[game["away"]].append((
                game["away_poss"], game["away_score"],
                game["home_poss"], game["home_score"],
            ))
            league_games.append(game)

    train = [r for r in predictions if r["season"] in TRAIN_SEASONS]
    holdout = [r for r in predictions if r["season"] == HOLDOUT_SEASON]
    if len(train) < 500 or len(holdout) < 100:
        raise RuntimeError(
            f"Insufficient chronological samples: train={len(train)}, holdout={len(holdout)}"
        )
    train_raw = summarize(train)
    offset = -float(train_raw["total_bias"])
    holdout_raw = summarize(holdout)
    holdout_calibrated = summarize(holdout, offset)
    holdout_probability_calibration = probability_calibration(
        holdout, train_raw["total_residual_sd"], offset
    )
    results = {
        "success": True,
        "model_version": "nba_season_to_date_efficiency_totals_v1",
        "method": (
            "Season-to-date team offensive/defensive points per possession and pace; "
            "team ratings shrunk toward the prior league environment with prior strength 8; "
            "2-point home-court margin split +1/-1 so it does not alter game total; "
            "same-day outcomes withheld."
        ),
        "source": "ramirobentes/nba_data public NBA play-by-play parquet",
        "files": files,
        "train_seasons": list(TRAIN_SEASONS),
        "holdout_season": HOLDOUT_SEASON,
        "train_raw": train_raw,
        "holdout_raw": holdout_raw,
        "calibration_fit_on_train_only": {
            "total_offset_points": round(offset, 4),
            "total_residual_sd_points": train_raw["total_residual_sd"],
            "training_games": len(train),
            "training_seasons": list(TRAIN_SEASONS),
            "holdout_season": HOLDOUT_SEASON,
        },
        "holdout_after_train_calibration": holdout_calibrated,
        "holdout_probability_calibration": holdout_probability_calibration,
        "holdout_predictions": [{
            "date": r["date"], "game_id": r["game_id"], "home": r["home"], "away": r["away"],
            "projected_total": round(r["total"], 3), "calibrated_total": round(r["total"] + offset, 3),
            "actual_total": r["actual_total"], "residual_after_calibration": round(r["total"] + offset - r["actual_total"], 3)
        } for r in holdout],
        "games_scored": len(predictions),
        "date_range": {"first": predictions[0]["date"], "last": predictions[-1]["date"]},
        "limitations": [
            "The holdout is a team-only season-to-date replay; historical pregame injury and player-impact snapshots are unavailable.",
            "Historical DraftKings full-game totals and paired executable prices are not in this free source; ROI and CLV are not measured.",
            "The live app adds current injury, rest and travel adjustments; those inputs are not replayed in this historical team-form test.",
        ],
    }
    with open(output_path, "w", encoding="utf-8") as output:
        json.dump(results, output, indent=2, allow_nan=False)
        output.write("\n")
    print("NBA_TOTALS_BACKTEST=" + json.dumps({
        "train": train_raw, "holdout": holdout_raw,
        "holdout_after_train_calibration": holdout_calibrated,
        "holdout_probability_calibration": holdout_probability_calibration,
        "training_only_offset": offset, "games": len(predictions),
    }, sort_keys=True))
    return results


if __name__ == "__main__":
    run()
