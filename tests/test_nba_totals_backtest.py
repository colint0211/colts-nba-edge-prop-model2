import unittest

from src.api import nba_totals_backtest


class NbaTotalsWalkForwardTests(unittest.TestCase):
    def test_home_court_changes_margin_not_combined_total(self):
        history = {
            "HOME": [(100.0, 115.0, 100.0, 110.0)] * 10,
            "AWAY": [(100.0, 112.0, 100.0, 108.0)] * 10,
        }
        result = nba_totals_backtest.predict(
            "HOME", "AWAY", history, {"ppp": 1.10, "pace": 99.0}
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            result["total"],
            result["home"] + result["away"],
            places=10,
        )
        # The +1/-1 home-court split must cancel from the total.
        home_rating = nba_totals_backtest.shrink_rating(
            nba_totals_backtest.team_rating(history["HOME"]),
            {"ppp": 1.10, "pace": 99.0},
        )
        away_rating = nba_totals_backtest.shrink_rating(
            nba_totals_backtest.team_rating(history["AWAY"]),
            {"ppp": 1.10, "pace": 99.0},
        )
        pace = (home_rating["pace"] + away_rating["pace"]) / 2
        home_eff = (home_rating["off"] + away_rating["def"]) / 2
        away_eff = (away_rating["off"] + home_rating["def"]) / 2
        self.assertAlmostEqual(result["total"], (home_eff + away_eff) * pace, places=8)

    def test_season_to_date_team_ratings_require_three_prior_games(self):
        self.assertIsNone(nba_totals_backtest.team_rating([(100, 110, 100, 108)] * 2))
        self.assertIsNotNone(nba_totals_backtest.team_rating([(100, 110, 100, 108)] * 3))

    def test_training_bias_offset_is_additive_and_changes_holdout_bias(self):
        rows = [{"total": 220.0, "actual_total": 221.0},
                {"total": 230.0, "actual_total": 231.0}]
        raw = nba_totals_backtest.summarize(rows)
        offset = -raw["total_bias"]
        calibrated = nba_totals_backtest.summarize(rows, offset)
        self.assertAlmostEqual(calibrated["total_bias"], 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
