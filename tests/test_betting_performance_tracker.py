import csv
import tempfile
import unittest
from pathlib import Path

from scripts.betting_performance_tracker import (
    american_profit, ingest, resolved_plays, settle, summarize
)


class TrackerTests(unittest.TestCase):
    def test_flat_one_unit_american_odds(self):
        self.assertAlmostEqual(american_profit(-110, "WIN"), 100 / 110)
        self.assertEqual(american_profit(-110, "LOSS"), -1)
        self.assertEqual(american_profit(-110, "PUSH"), 0)

    def test_ingest_keeps_plays_and_skips_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "input.csv"
            ledger = Path(td) / "events.csv"
            fields = ["play_id", "model", "qualified_at", "sportsbook", "market",
                      "selection", "line", "odds_american", "model_probability",
                      "result", "source_record_id", "qualification_rule_version",
                      "verification_status"]
            row = {"play_id":"p1", "model":"nba_ats",
                   "qualified_at":"2026-11-10T19:00:00-05:00",
                   "sportsbook":"DraftKings", "market":"spread",
                   "selection":"away +4.5", "line":"+4.5",
                   "odds_american":"-110", "model_probability":"0.56",
                   "result":"PENDING", "source_record_id":"src-1",
                   "qualification_rule_version":"v1",
                   "verification_status":"VERIFIED"}
            with src.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(ingest(src, ledger)["ingested"], 1)
            self.assertEqual(ingest(src, ledger)["duplicates_skipped"], 1)
            self.assertEqual(len(resolved_plays(ledger)), 1)
            settle(ledger, "p1", "LOSS", None)
            self.assertEqual(summarize(ledger)["net_units"], -1.0)
            with self.assertRaises(SystemExit):
                settle(ledger, "p1", "WIN", None)


if __name__ == "__main__":
    unittest.main()
