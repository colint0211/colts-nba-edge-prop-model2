import unittest

from src.api.game_model import _candidate


class GameCandidateProbabilityTests(unittest.TestCase):
    def _model(self, margin):
        return {
            "projected_home": 115.0,
            "projected_away": 110.0,
            "projected_margin": margin,
            "projected_total": 225.0,
            "projected_pace": 99.0,
            "home_adjustments": {},
            "away_adjustments": {},
            "score_distribution": {
                "margin_sd": 14.8,
                "total_sd": 19.0,
                "moneyline_logistic_scale": 8.17,
            },
            "model_stage": "test",
            "probability_calibration_status": "provisional_not_validated",
        }

    def _game(self):
        return {
            "event_id": "test-event",
            "matchup": "Away @ Home",
            "home": "BOS",
            "away": "NYK",
            "commence_time": "2026-11-12T00:00:00Z",
            "spreads": {
                "BOS": {"line": -2.5, "odds": -110},
                "NYK": {"line": 2.5, "odds": -110},
            },
            "totals": {
                "over": {"line": 220.5, "odds": -110},
                "under": {"line": 220.5, "odds": -110},
            },
        }

    def test_positive_home_margin_means_home_win_probability_above_half(self):
        rows = _candidate(self._game(), "sides", self._model(5.0))
        home = next(row for row in rows if row["selection"] == "BOS")
        away = next(row for row in rows if row["selection"] == "NYK")
        self.assertGreater(home["home_win_probability"], 0.5)
        self.assertLess(home["away_win_probability"], 0.5)
        self.assertAlmostEqual(home["home_win_probability"] + home["away_win_probability"], 1.0)
        self.assertEqual(home["home_win_probability"], away["home_win_probability"])

    def test_negative_home_margin_means_away_win_probability_above_half(self):
        rows = _candidate(self._game(), "sides", self._model(-5.0))
        home = next(row for row in rows if row["selection"] == "BOS")
        self.assertLess(home["home_win_probability"], 0.5)
        self.assertGreater(home["away_win_probability"], 0.5)


if __name__ == "__main__":
    unittest.main()
