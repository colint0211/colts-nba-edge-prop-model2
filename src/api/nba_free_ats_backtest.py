"""Free-sample NBA closing-spread benchmark.

Uses The Odds Gap's publicly downloadable October 19-25, 2026 sample week.
No account, key, paid subscription, or credit balance is required. The feed is
a consensus closing spread, not a DraftKings-specific quote; this module
therefore reports forecast-vs-close error and directional ATS results only,
never DraftKings ROI or exact-price EV.
"""
import csv
import io
import json
import math
from urllib.request import Request, urlopen

SOURCE_URL = (
    "https://theoddsgap.com/api/team-results-export"
    "?league=nba&start=2026-10-19&end=2026-10-25&format=csv"
)
SAMPLE_START = "2026-10-19"
SAMPLE_END = "2026-10-25"

TEAM_NAMES = {
    "atlanta hawks": "ATL", "boston celtics": "BOS",
    "brooklyn nets": "BKN", "new jersey nets": "BKN",
    "charlotte hornets": "CHA", "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE", "dallas mavericks": "DAL",
    "denver nuggets": "DEN", "detroit pistons": "DET",
    "golden state warriors": "GSW", "houston rockets": "HOU",
    "indiana pacers": "IND", "los angeles clippers": "LAC",
    "la clippers": "LAC", "los angeles lakers": "LAL",
    "la lakers": "LAL", "memphis grizzlies": "MEM",
    "miami heat": "MIA", "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN", "new orleans pelicans": "NOP",
    "new york knicks": "NYK", "oklahoma city thunder": "OKC",
    "orlando magic": "ORL", "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX", "portland trail blazers": "POR",
    "sacramento kings": "SAC", "san antonio spurs": "SAS",
    "toronto raptors": "TOR", "utah jazz": "UTA",
    "washington wizards": "WAS",
}


def canonical_team(value):
    raw = str(value or "").strip().upper()
    if raw in set(TEAM_NAMES.values()):
        return raw
    return TEAM_NAMES.get(str(value or "").strip().lower())


def fetch_rows():
    request = Request(SOURCE_URL, headers={
        "Accept": "text/csv",
        "User-Agent": "ColtsNBAEdge/2.0",
    })
    with urlopen(request, timeout=35) as response:
        body = response.read().decode("utf-8-sig", "replace")
        headers = {k.lower(): v for k, v in response.headers.items()}
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        raise RuntimeError("Free NBA closing-line sample returned no rows.")
    dates = {str(row.get("game_date", "")) for row in rows}
    dates.discard("")
    if not dates or not dates.issubset({f"2026-10-{day:02d}" for day in range(19, 26)}):
        raise RuntimeError(
            "Free data endpoint did not return the documented Oct 19-25, 2026 sample; "
            f"received dates {sorted(dates)[:3]} through {sorted(dates)[-3:] if dates else []}."
        )
    return rows, headers


def build_home_index(rows):
    index = {}
    for row in rows:
        if str(row.get("venue", "")).strip().lower() != "home":
            continue
        home = canonical_team(row.get("team"))
        away = canonical_team(row.get("opponent"))
        if not home or not away:
            continue
        key = (str(row.get("game_date", "")), home, away)
        index[key] = row
    return index


def score_predictions(predictions, rows):
    home_index = build_home_index(rows)
    matched = []
    for prediction in predictions:
        key = (
            str(prediction.get("date", "")),
            canonical_team(prediction.get("home")),
            canonical_team(prediction.get("away")),
        )
        row = home_index.get(key)
        if not row:
            continue
        try:
            line = float(row["close_spread"])
            actual_home = float(row["team_score"])
            actual_away = float(row["opp_score"])
        except (KeyError, TypeError, ValueError):
            continue
        actual_margin = actual_home - actual_away
        # Reject a join unless the independent source's final score agrees.
        if (actual_home != float(prediction["actual_home"])
                or actual_away != float(prediction["actual_away"])):
            continue
        model_margin = float(prediction["calibrated_margin"])
        market_margin = -line
        edge = model_margin - market_margin
        cover_margin = actual_margin + line
        if abs(cover_margin) < 1e-9:
            settlement = "PUSH"
        elif edge > 0:
            settlement = "WIN" if cover_margin > 0 else "LOSS"
        elif edge < 0:
            settlement = "WIN" if cover_margin < 0 else "LOSS"
        else:
            settlement = "NO_EDGE"
        matched.append({
            "date": key[0], "game_id": prediction.get("game_id"),
            "matchup": f"{key[2]} @ {key[1]}",
            "model_margin": round(model_margin, 3),
            "market_implied_home_margin": round(market_margin, 3),
            "closing_home_spread": line,
            "model_market_gap_points": round(edge, 3),
            "actual_home_margin": actual_margin,
            "model_margin_error": round(model_margin - actual_margin, 3),
            "market_margin_error": round(market_margin - actual_margin, 3),
            "model_side": "home" if edge > 0 else ("away" if edge < 0 else "pass"),
            "ats_settlement": settlement,
            "close_source": row.get("close_source"),
            "close_books": row.get("close_books"),
        })
    return matched


def rmse(values):
    return round(math.sqrt(sum(x * x for x in values) / len(values)), 3) if values else None


def threshold_records(games, thresholds=(2.5, 4.0, 5.0)):
    output = {}
    for threshold in thresholds:
        selected = [g for g in games if abs(g["model_market_gap_points"]) >= threshold]
        decided = [g for g in selected if g["ats_settlement"] in ("WIN", "LOSS")]
        wins = sum(g["ats_settlement"] == "WIN" for g in decided)
        losses = sum(g["ats_settlement"] == "LOSS" for g in decided)
        pushes = sum(g["ats_settlement"] == "PUSH" for g in selected)
        output[str(threshold)] = {
            "minimum_model_market_gap_points": threshold,
            "bets": len(selected), "wins": wins, "losses": losses, "pushes": pushes,
            "win_rate_pct_ex_push": round(100 * wins / len(decided), 2) if decided else None,
            "note": "Directional ATS record only; no book-specific price or ROI is available in this free consensus sample.",
        }
    return output


def run(input_path="nba_live_aligned_backtest.json", output_path="nba_free_ats_backtest.json"):
    with open(input_path, "r", encoding="utf-8") as source:
        aligned = json.load(source)
    predictions = aligned.get("holdout_predictions") or []
    rows, headers = fetch_rows()
    games = score_predictions(predictions, rows)
    if not games:
        raise RuntimeError("No score-verified overlap between holdout predictions and free closing-line sample.")
    model_errors = [g["model_margin_error"] for g in games]
    market_errors = [g["market_margin_error"] for g in games]
    result = {
        "status": "complete",
        "source": "The Odds Gap free NBA team-results sample, consensus closing spread",
        "source_url": SOURCE_URL,
        "price": "free; no signup, API key, paid subscription, or credits",
        "sample_window": {"start": SAMPLE_START, "end": SAMPLE_END},
        "holdout_season": aligned.get("holdout_season"),
        "predictions_in_sample_window": sum(SAMPLE_START <= str(p.get("date", "")) <= SAMPLE_END for p in predictions),
        "sample_rows": len(rows),
        "score_verified_games_matched": len(games),
        "model_margin_rmse": rmse(model_errors),
        "closing_market_margin_rmse": rmse(market_errors),
        "model_minus_market_rmse_points": round(rmse(model_errors) - rmse(market_errors), 3),
        "mean_model_market_gap_points": round(sum(g["model_market_gap_points"] for g in games) / len(games), 3),
        "directional_ats_by_gap": threshold_records(games),
        "games": games,
        "response_data_tier": headers.get("x-access-tier"),
        "limitations": [
            "This free source provides consensus closing spreads, not DraftKings-only lines or paired DraftKings odds.",
            "Only the documented free sample week, October 19-25, 2026, is available without payment; it covers only a subset of the 160-game holdout.",
            "No ROI, EV, or exact-price break-even claim is made because this source's team-results file does not provide executable side prices.",
            "The tested forecast is the injury-free, team-form baseline; historical pregame injury and player-impact adjustments are not replayed.",
        ],
    }
    with open(output_path, "w", encoding="utf-8") as output:
        json.dump(result, output, indent=2, allow_nan=False)
    print("FREE_ATS_BACKTEST=" + json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    run()
