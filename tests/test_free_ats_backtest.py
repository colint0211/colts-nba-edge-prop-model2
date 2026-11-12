import unittest

from src.api.nba_free_ats_backtest import (
    canonical_team,
    score_predictions,
    threshold_records,
)


class FreeAtsBacktestTests(unittest.TestCase):
    def test_team_name_aliases(self):
        self.assertEqual(canonical_team("Los Angeles Lakers"), "LAL")
        self.assertEqual(canonical_team("New York Knicks"), "NYK")
        self.assertEqual(canonical_team("GSW"), "GSW")
        self.assertIsNone(canonical_team("Unknown Team"))

    def test_home_rows_join_by_date_teams_and_score(self):
        rows = [{
            "game_date": "2026-10-20",
            "venue": "home",
            "team": "Detroit Pistons",
            "opponent": "Boston Celtics",
            "team_score": "121",
            "opp_score": "113",
            "close_spread": "-2.5",
            "close_source": "consensus_close",
            "close_books": "20",
        }]
        predictions = [{
            "date": "2026-10-20", "game_id": "game-1",
            "home": "DET", "away": "BOS",
            "actual_home": 121, "actual_away": 113,
            "actual_margin": 8, "calibrated_margin": 4.0,
        }]
        games = score_predictions(predictions, rows)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["closing_home_spread"], -2.5)
        self.assertEqual(games[0]["market_implied_home_margin"], 2.5)
        self.assertEqual(games[0]["model_market_gap_points"], 1.5)
        self.assertEqual(games[0]["ats_settlement"], "WIN")

    def test_score_mismatch_is_rejected(self):
        rows = [{
            "game_date": "2026-10-20", "venue": "home",
            "team": "Detroit Pistons", "opponent": "Boston Celtics",
            "team_score": "120", "opp_score": "113", "close_spread": "-2.5",
        }]
        predictions = [{
            "date": "2026-10-20", "home": "DET", "away": "BOS",
            "actual_home": 121, "actual_away": 113,
            "calibrated_margin": 2.0,
        }]
        self.assertEqual(score_predictions(predictions, rows), [])

    def test_gap_threshold_reports_directional_results_without_roi(self):
        games = [
            {"model_market_gap_points": 3.0, "ats_settlement": "WIN"},
            {"model_market_gap_points": -4.0, "ats_settlement": "LOSS"},
            {"model_market_gap_points": 1.0, "ats_settlement": "WIN"},
        ]
        summary = threshold_records(games, thresholds=(2.5,))
        self.assertEqual(summary["2.5"]["bets"], 2)
        self.assertEqual(summary["2.5"]["wins"], 1)
        self.assertEqual(summary["2.5"]["losses"], 1)
        self.assertNotIn("roi_pct", summary["2.5"])


if __name__ == "__main__":
    unittest.main()
