import unittest

from src.api import nba_totals


class NbaTotalsTests(unittest.TestCase):
    def test_parser_needs_only_draftkings_full_game_totals(self):
        events = [{
            "id": "nba-game-1",
            "commence_time": "2026-11-24T00:00:00Z",
            "away_team": "Boston Celtics",
            "home_team": "New York Knicks",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "totals",
                    "line_type": "main",
                    "period": "full_game",
                    "last_update": "2026-11-23T17:00:00Z",
                    "outcomes": [
                        {"name": "Over", "point": 222.5, "price": -110},
                        {"name": "Under", "point": 222.5, "price": -110},
                    ],
                }],
            }],
        }]
        rows = nba_totals._parse_total_lines(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["totals"]["over"]["line"], 222.5)
        self.assertEqual(rows[0]["totals"]["under"]["odds"], -110)

    def test_parser_rejects_alternate_total(self):
        events = [{
            "id": "nba-game-2", "commence_time": "2026-11-24T00:00:00Z",
            "away_team": "Boston Celtics", "home_team": "New York Knicks",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "totals", "line_type": "alternate",
                    "outcomes": [
                        {"name": "Over", "point": 230.5, "price": 120},
                        {"name": "Under", "point": 230.5, "price": -140},
                    ],
                }],
            }],
        }]
        self.assertEqual(nba_totals._parse_total_lines(events), [])

    def test_integer_total_has_explicit_push_probability(self):
        result = nba_totals._normal_probabilities(225.0, 17.0, 225.0)
        self.assertGreater(result["push"], 0.0)
        self.assertAlmostEqual(result["over"] + result["under"], 1.0, places=10)
        self.assertAlmostEqual(result["over_win"] + result["under_win"] + result["push"], 1.0, places=10)

    def test_half_point_total_has_no_push_probability(self):
        result = nba_totals._normal_probabilities(225.0, 17.0, 224.5)
        self.assertEqual(result["push"], 0.0)
        self.assertAlmostEqual(result["over"] + result["under"], 1.0, places=10)

    def test_minimum_price_targets_three_percent_expected_value(self):
        price = nba_totals._minimum_odds(0.5, 0.5, 0.03)
        payout = price / 100.0 if price > 0 else 100.0 / abs(price)
        expected_value = 0.5 * payout - 0.5
        self.assertAlmostEqual(expected_value, 0.03, delta=0.005)

    def test_fair_odds_are_independent_of_market_prices(self):
        self.assertEqual(nba_totals._american_odds(0.5), -100)
        self.assertEqual(nba_totals._american_odds(0.4), 150)


if __name__ == "__main__":
    unittest.main()
