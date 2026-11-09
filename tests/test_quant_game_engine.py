import unittest

from src.api.quant_game_engine import project_matchup


class QuantGameEngineTests(unittest.TestCase):
    def setUp(self):
        self.league = {
            "A": {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20},
            "B": {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20},
            "C": {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20},
        }

    def test_equal_teams_produce_home_court_margin(self):
        team = {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20}
        result = project_matchup(home=team, away=team, all_teams=self.league)
        self.assertAlmostEqual(result["projected_margin"], 2.0, places=2)
        self.assertAlmostEqual(result["projected_total"], 232.0, places=2)

    def test_opponent_weak_defense_increases_scoring_projection(self):
        home = {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20}
        away = {"ortg": 115.0, "drtg": 120.0, "pace": 100.0, "games": 20}
        result = project_matchup(home=home, away=away, all_teams=self.league)
        self.assertGreater(result["projected_home"], 117.0)

    def test_early_season_uncertainty_is_wider(self):
        mature = {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20}
        early = {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 2}
        mature_result = project_matchup(home=mature, away=mature, all_teams=self.league)
        early_result = project_matchup(home=early, away=early, all_teams=self.league,
                                        data_quality="derived_early_season")
        self.assertGreater(early_result["score_distribution"]["margin_sd"],
                           mature_result["score_distribution"]["margin_sd"])

    def test_situational_adjustments_change_scores(self):
        team = {"ortg": 115.0, "drtg": 115.0, "pace": 100.0, "games": 20}
        result = project_matchup(home=team, away=team, all_teams=self.league,
                                 home_adjustment=-2.0, away_adjustment=1.0)
        self.assertAlmostEqual(result["projected_margin"], -1.0, places=2)
        self.assertAlmostEqual(result["projected_total"], 231.0, places=2)

    def test_invalid_pace_is_rejected(self):
        team = {"ortg": 115.0, "drtg": 115.0, "pace": 0.0, "games": 20}
        with self.assertRaises(ValueError):
            project_matchup(home=team, away=team, all_teams=self.league)


if __name__ == "__main__":
    unittest.main()
