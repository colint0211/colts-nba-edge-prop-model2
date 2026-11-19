"""Tests for the DraftKings-only Benter quote archive."""
import unittest

from src.api.benter_archive import _extract_dk_quotes


class BenterArchiveTests(unittest.TestCase):
    def test_archives_only_dk_with_both_sides_at_identical_main_line(self):
        events = [{
            "id": "game-1",
            "away_team": "Boston Celtics",
            "home_team": "New York Knicks",
            "commence_time": "2026-11-19T00:00:00Z",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [{
                        "key": "player_points",
                        "line_type": "main",
                        "last_update": "2026-11-18T22:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Jayson Tatum (BOS)", "point": 27.5, "price": -110},
                            {"name": "Under", "description": "Jayson Tatum (BOS)", "point": 27.5, "price": -110},
                            {"name": "Over", "description": "Jaylen Brown (BOS)", "point": 22.5, "price": -105},
                        ],
                    }],
                },
                {
                    "key": "fanduel",
                    "markets": [{
                        "key": "player_points",
                        "line_type": "main",
                        "outcomes": [
                            {"name": "Over", "description": "Jayson Tatum (BOS)", "point": 27.5, "price": -105},
                            {"name": "Under", "description": "Jayson Tatum (BOS)", "point": 27.5, "price": -115},
                        ],
                    }],
                },
            ],
        }]

        quotes = _extract_dk_quotes(events)
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        self.assertEqual(quote["player"], "Jayson Tatum")
        self.assertEqual(quote["team"], "BOS")
        self.assertEqual(quote["market"], "points")
        self.assertEqual(quote["line"], 27.5)
        self.assertEqual(quote["over_odds"], -110)
        self.assertEqual(quote["under_odds"], -110)
        self.assertEqual(quote["bookmaker"], "DraftKings")

    def test_accepts_threes_and_ignores_non_main_markets(self):
        events = [{
            "id": "game-2",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [
                    {
                        "key": "player_threes",
                        "line_type": "main",
                        "outcomes": [
                            {"name": "Over", "description": "Stephen Curry", "point": 4.5, "price": 105},
                            {"name": "Under", "description": "Stephen Curry", "point": 4.5, "price": -125},
                        ],
                    },
                    {
                        "key": "player_points",
                        "line_type": "alternate",
                        "outcomes": [
                            {"name": "Over", "description": "Stephen Curry", "point": 30.5, "price": 100},
                            {"name": "Under", "description": "Stephen Curry", "point": 30.5, "price": -120},
                        ],
                    },
                ],
            }],
        }]

        quotes = _extract_dk_quotes(events)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["market"], "threes")
        self.assertEqual(quotes[0]["line"], 4.5)


if __name__ == "__main__":
    unittest.main()
