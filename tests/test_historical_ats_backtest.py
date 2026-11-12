import unittest

from src.api import nba_historical_ats_backtest as ats


class HistoricalATSBacktestTests(unittest.TestCase):
    def test_team_aliases_normalize_provider_codes(self):
        self.assertEqual(ats.canonical_team("GS"), "GSW")
        self.assertEqual(ats.canonical_team("NY"), "NYK")
        self.assertEqual(ats.canonical_team("SA"), "SAS")

    def test_half_point_cover_probability_has_no_push(self):
        win, push = ats.cover_probabilities(5.0, -2.5, 10.0)
        self.assertGreater(win, 0.5)
        self.assertEqual(push, 0.0)

    def test_integer_spread_has_push_mass(self):
        win, push = ats.cover_probabilities(0.0, -3.0, 10.0)
        self.assertGreater(push, 0.0)
        self.assertLess(win + push, 1.0)

    def test_draftkings_close_quote_is_required(self):
        game = {"books": [
            {"provider": "DraftKings", "home_spread_close": None,
             "spread_home_odds": -110, "spread_away_odds": -110},
            {"provider": "FanDuel", "home_spread_close": -3.5,
             "spread_home_odds": -110, "spread_away_odds": -110},
        ]}
        self.assertIsNone(ats._draftkings_quote(game))

    def test_draftkings_close_quote_parses(self):
        game = {"books": [{
            "provider": "DraftKings Sportsbook", "home_spread_close": -4.5,
            "spread_home_odds": -112, "spread_away_odds": -108,
        }]}
        quote = ats._draftkings_quote(game)
        self.assertEqual(quote["line"], -4.5)
        self.assertEqual(quote["home_odds"], -112)

    def test_push_does_not_count_as_win_or_loss(self):
        metrics = ats.summarize([
            {"outcome": "WIN", "profit_units": 0.91, "model_probability": 0.6, "edge_pp": 5, "ev_pct": 4},
            {"outcome": "PUSH", "profit_units": 0.0, "model_probability": 0.6, "edge_pp": 5, "ev_pct": 4},
        ])
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["pushes"], 1)
        self.assertEqual(metrics["win_rate_pct_ex_push"], 100.0)


if __name__ == "__main__":
    unittest.main()
