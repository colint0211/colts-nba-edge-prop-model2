import unittest
from unittest.mock import patch

from datetime import datetime, timezone\nfrom src.api import game_model, prediction_ledger


class DailySlateAuditTests(unittest.TestCase):
    @patch("src.api.prediction_ledger.get_daily_slate")
    def test_reports_saved_timestamps_and_candidate_counts_without_filling_missing_rows(self, get_daily_slate):
        def fake_get(market, slate_date):
            self.assertEqual(slate_date, "2026-11-18")
            if market == "points":
                return {"market": market, "slate_date": slate_date, "created_at": "2026-11-18T12:05:00+00:00", "candidates": [{}, {}, {}]}
            if market == "rebounds":
                return {"market": market, "slate_date": slate_date, "created_at": "2026-11-18T12:06:00+00:00", "candidates": []}
            return None

        get_daily_slate.side_effect = fake_get
        result = prediction_ledger.audit_daily_slates("2026-11-18")

        self.assertTrue(result["read_only"])
        self.assertEqual(result["timezone"], "America/New_York")
        self.assertTrue(result["markets"]["points"]["locked"])
        self.assertEqual(result["markets"]["points"]["candidate_count"], 3)
        self.assertEqual(result["markets"]["points"]["created_at"], "2026-11-18T12:05:00+00:00")
        self.assertTrue(result["markets"]["rebounds"]["locked"])
        self.assertEqual(result["markets"]["rebounds"]["candidate_count"], 0)
        self.assertFalse(result["markets"]["assists"]["locked"])
        self.assertIsNone(result["markets"]["assists"]["candidate_count"])
        self.assertEqual(result["markets"]["assists"]["status"], "missing")
        self.assertFalse(result["markets"]["sides"]["supported"])
        self.assertFalse(result["markets"]["totals"]["supported"])
        self.assertEqual(get_daily_slate.call_count, 5)


    @patch("src.api.game_model._connect")
    def test_audits_side_total_and_experimental_snapshots_without_settlement(self, connect):
        class Cursor:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.rows[0] if self.rows else None

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, params):
                if "FROM nba_game_slates" in query:
                    return Cursor([
                        ("sides", [{"selection": "BOS"}], datetime(2026, 11, 18, 12, 7, tzinfo=timezone.utc)),
                        ("totals", [], datetime(2026, 11, 18, 12, 8, tzinfo=timezone.utc)),
                    ])
                if "FROM nba_experimental_totals" in query:
                    return Cursor([(
                        2,
                        datetime(2026, 11, 18, 12, 9, tzinfo=timezone.utc),
                        datetime(2026, 11, 18, 12, 10, tzinfo=timezone.utc),
                    )])
                raise AssertionError("Unexpected query")

        connect.return_value = Connection()
        result = game_model.audit_game_slates("2026-11-18")

        self.assertTrue(result["sides"]["locked"])
        self.assertEqual(result["sides"]["candidate_count"], 1)
        self.assertEqual(result["sides"]["classification"], "research_only")
        self.assertFalse(result["totals"]["locked"])
        self.assertEqual(result["totals"]["candidate_count"], 0)
        self.assertEqual(result["totals"]["status"], "disabled_empty_placeholder")
        self.assertEqual(result["experimental_totals"]["candidate_count"], 2)
        self.assertEqual(result["experimental_totals"]["status"], "research_only_saved")

    def test_rejects_invalid_slate_date(self):
        with self.assertRaises(ValueError):
            prediction_ledger.audit_daily_slates("today")


if __name__ == "__main__":
    unittest.main()
