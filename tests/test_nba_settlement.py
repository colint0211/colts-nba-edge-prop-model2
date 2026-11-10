import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.api.index import _candidate_is_pregame


class PregameSlateValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 7, 5, tzinfo=ZoneInfo("America/New_York"))

    def test_accepts_future_tipoff_on_the_eastern_slate_date(self):
        prop = {"game_start": "2026-11-10T19:30:00-05:00"}
        self.assertTrue(_candidate_is_pregame(prop, "2026-11-10", self.now))

    def test_rejects_previous_days_game_mislabeled_as_today(self):
        prop = {"game_start": "2026-11-09T19:00:00-05:00"}
        self.assertFalse(_candidate_is_pregame(prop, "2026-11-10", self.now))

    def test_rejects_a_game_after_tipoff(self):
        prop = {"game_start": "2026-11-10T19:00:00-05:00"}
        now = datetime(2026, 11, 10, 19, 1, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_candidate_is_pregame(prop, "2026-11-10", now))

    def test_rejects_missing_or_timezone_naive_tipoff(self):
        self.assertFalse(_candidate_is_pregame({}, "2026-11-10", self.now))
        self.assertFalse(
            _candidate_is_pregame(
                {"game_start": "2026-11-10T19:30:00"},
                "2026-11-10",
                self.now,
            )
        )

    def test_converts_utc_tipoff_before_comparing_slate_date(self):
        prop = {"game_start": "2026-11-11T00:30:00Z"}
        self.assertTrue(_candidate_is_pregame(prop, "2026-11-10", self.now))


if __name__ == "__main__":
    unittest.main()
