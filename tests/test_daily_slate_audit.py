import unittest
from unittest.mock import patch

from src.api import prediction_ledger


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

    def test_rejects_invalid_slate_date(self):
        with self.assertRaises(ValueError):
            prediction_ledger.audit_daily_slates("today")


if __name__ == "__main__":
    unittest.main()
