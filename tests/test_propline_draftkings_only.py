import os
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch

from fastapi import HTTPException

from src.api import index


class FreeOnlyProviderGuardTests(unittest.TestCase):
    def test_propline_is_blocked_before_any_network_request(self):
        with patch.dict(os.environ, {"PROPLINE_API_KEY": "test-key"}):
            with patch.object(index, "urlopen") as request:
                with self.assertRaises(HTTPException) as raised:
                    index._propline_fetch_events()
        self.assertEqual(raised.exception.status_code, 503)
        request.assert_not_called()

    def test_bettingpros_props_are_blocked_before_any_network_request(self):
        with patch.dict(os.environ, {"BETTINGPROS_API_KEY": "test-key"}):
            with patch.object(index, "urlopen") as request:
                with self.assertRaises(HTTPException) as raised:
                    index._bp_fetch_props("points")
        self.assertEqual(raised.exception.status_code, 503)
        request.assert_not_called()

    def test_bettingpros_books_and_offers_helpers_never_fetch(self):
        with patch.dict(os.environ, {"BETTINGPROS_API_KEY": "test-key"}):
            with patch.object(index, "urlopen") as request:
                self.assertEqual(index._bp_books(), {})
                self.assertEqual(index._bp_event_offers("event-1", "points"), [])
                self.assertEqual(index._bp_event_matchup("event-1"), "")
        request.assert_not_called()

    def test_public_bettingpros_routes_fail_closed(self):
        with patch.dict(os.environ, {"BETTINGPROS_API_KEY": "test-key"}):
            with patch.object(index, "urlopen") as request:
                with self.assertRaises(HTTPException) as books_error:
                    index.bettingpros_books()
                with self.assertRaises(HTTPException) as offers_error:
                    index.bettingpros_offers("points")
        self.assertEqual(books_error.exception.status_code, 503)
        self.assertEqual(offers_error.exception.status_code, 503)
        request.assert_not_called()

    def test_tank01_is_blocked_before_any_network_request(self):
        with patch.dict(os.environ, {"TANK01_API_KEY": "test-key"}):
            with patch.object(index, "urlopen") as request:
                with self.assertRaises(RuntimeError):
                    index._tank01_request("/getNBATeams")
                response = index.tank01_dvp_probe()
        self.assertEqual(response.status_code, 503)
        request.assert_not_called()

    def test_api_sports_is_blocked_before_any_network_request(self):
        with patch.dict(os.environ, {"API_SPORTS_KEY": "test-key"}):
            with patch.object(index, "urlopen") as request:
                self.assertIsNone(index._api_sports_player_position("Jalen Brunson"))
                response = index.api_sports_status()
        self.assertEqual(response.status_code, 503)
        request.assert_not_called()


class DraftKingsPropNormalizationTests(unittest.TestCase):
    def test_draftkings_market_normalization_remains_pure(self):
        events = [{
            "id": "game-1",
            "commence_time": "2026-11-27T00:00:00Z",
            "away_team": "New York Knicks",
            "home_team": "Miami Heat",
            "bookmakers": [{
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [{
                    "key": "player_points",
                    "line_type": "main",
                    "outcomes": [
                        {"name": "Over", "description": "Jalen Brunson", "point": 27.5, "price": -115},
                        {"name": "Under", "description": "Jalen Brunson", "point": 27.5, "price": -105},
                    ],
                }],
            }],
        }]
        props = index._propline_market_props(events, "points")
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["player"], "Jalen Brunson")
        self.assertEqual(props[0]["over_odds"], -115)
        self.assertEqual(props[0]["under_odds"], -105)
        self.assertEqual(props[0]["market_books"], ["DraftKings"])

    def test_filter_rejects_past_and_wrong_date_events(self):
        now_utc = datetime(2026, 11, 20, 12, 5, tzinfo=timezone.utc)
        props = [
            {"event_id": "tonight", "commence_time": "2026-11-21T00:00:00Z"},
            {"event_id": "yesterday", "commence_time": "2026-11-20T02:00:00Z"},
            {"event_id": "tomorrow", "commence_time": "2026-11-21T23:00:00Z"},
            {"event_id": "missing"},
            {"event_id": "naive", "commence_time": "2026-11-20T20:00:00"},
        ]
        filtered = index._filter_props_for_slate(props, "2026-11-20", now_utc=now_utc)
        self.assertEqual([row["event_id"] for row in filtered], ["tonight"])

    def test_empty_or_stale_board_is_not_reported_as_frozen(self):
        now_et = datetime(2026, 11, 20, 7, 5, tzinfo=ZoneInfo("America/New_York"))
        with patch("src.api.index.datetime") as mock_datetime:
            mock_datetime.now.return_value = now_et
            with patch.object(index, "_active_nba_slate_date", return_value="2026-11-20"):
                with patch.object(index.prediction_ledger, "initialize"):
                    with patch.object(index.prediction_ledger, "get_daily_slate", return_value=None):
                        with patch.object(index.prediction_ledger, "freeze_daily_slate") as freeze:
                            result = index._snapshot_daily_market_top5("points", [
                                {"player": "old-game-player", "commence_time": "2026-11-20T02:00:00Z"}
                            ])
        self.assertIsNone(result)
        freeze.assert_not_called()

    def test_empty_board_is_not_frozen(self):
        now_et = datetime(2026, 11, 20, 7, 5, tzinfo=ZoneInfo("America/New_York"))
        with patch("src.api.index.datetime") as mock_datetime:
            mock_datetime.now.return_value = now_et
            with patch.object(index, "_active_nba_slate_date", return_value="2026-11-20"):
                with patch.object(index.prediction_ledger, "initialize"):
                    with patch.object(index.prediction_ledger, "get_daily_slate", return_value=None):
                        with patch.object(index.prediction_ledger, "freeze_daily_slate") as freeze:
                            result = index._snapshot_daily_market_top5("points", [])
        self.assertIsNone(result)
        freeze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
