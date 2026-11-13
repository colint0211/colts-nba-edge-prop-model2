import json
import os
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from src.api import index


class DraftKingsOnlyPropLineTests(unittest.TestCase):
    def test_live_request_asks_prop_line_for_draftkings_only(self):
        class FakeResponse:
            headers = {"X-Daily-Limit": "100", "X-Daily-Used": "0", "X-Daily-Remaining": "100"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return b'{"events": []}'

        def fake_urlopen(request, timeout=20):
            query = parse_qs(urlparse(request.full_url).query)
            self.assertEqual(query.get("bookmakers"), ["draftkings"])
            self.assertEqual(timeout, 20)
            return FakeResponse()

        with patch.dict(os.environ, {"PROPLINE_API_KEY": "unit-test-only"}):
            with patch.object(index, "_PROPLINE_CACHE", {"at": 0.0, "events": None, "headers": {}}):
                with patch.object(index, "_PROPLINE_COOLDOWN_UNTIL", 0.0):
                    with patch.object(index, "urlopen", side_effect=fake_urlopen):
                        events, source, headers = index._propline_fetch_events()

        self.assertEqual(events, [])
        self.assertEqual(source, "live")
        self.assertEqual(headers.get("daily_remaining"), "100")

    def test_fanduel_quotes_are_ignored_even_when_they_are_better(self):
        events = [{
            "id": "game-1",
            "away_team": "New York Knicks",
            "home_team": "Indiana Pacers",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [{
                        "key": "player_points",
                        "line_type": "main",
                        "last_update": "2026-11-13T11:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Jalen Brunson", "point": 27.5, "price": -115},
                            {"name": "Under", "description": "Jalen Brunson", "point": 27.5, "price": -105},
                        ],
                    }],
                },
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [{
                        "key": "player_points",
                        "line_type": "main",
                        "last_update": "2026-11-13T11:01:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Jalen Brunson", "point": 27.5, "price": 110},
                            {"name": "Under", "description": "Jalen Brunson", "point": 27.5, "price": -130},
                            {"name": "Over", "description": "FanDuel-only player", "point": 10.5, "price": -110},
                            {"name": "Under", "description": "FanDuel-only player", "point": 10.5, "price": -110},
                        ],
                    }],
                },
            ],
        }]

        props = index._propline_market_props(events, "points")

        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["player"], "Jalen Brunson")
        self.assertEqual(props[0]["over_odds"], -115)
        self.assertEqual(props[0]["under_odds"], -105)
        self.assertEqual(props[0]["best_over_book"], "DraftKings")
        self.assertEqual(props[0]["best_under_book"], "DraftKings")
        self.assertEqual(props[0]["market_books"], ["DraftKings"])


if __name__ == "__main__":
    unittest.main()
