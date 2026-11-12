import unittest

from src.api.index import _apply_projection_side_guard, _projection_side_review


class ProjectionSideReviewTests(unittest.TestCase):
    def test_projection_above_line_selects_over_and_reprices_it(self):
        prop = {
            "projection": 4.0, "line": 3.5, "best_side": "UNDER",
            "over_probability": 65.0, "under_probability": 35.0,
            "over_odds": -110, "under_odds": 110,
            "expected_value_pct": 20.0,
        }
        _apply_projection_side_guard(prop)
        self.assertEqual(prop["best_side"], "OVER")
        self.assertEqual(prop["best_probability"], 65.0)
        self.assertEqual(prop["best_odds"], -110)
        self.assertAlmostEqual(prop["expected_value_pct"], 24.09, places=1)
        self.assertEqual(prop["recommendation"], "VALUE CANDIDATE")
        self.assertFalse(prop["projection_review_required"])

    def test_projection_below_line_selects_under_and_reprices_it(self):
        prop = {
            "projection": 7.0, "line": 7.5, "best_side": "OVER",
            "over_probability": 35.0, "under_probability": 65.0,
            "over_odds": 110, "under_odds": -110,
        }
        _apply_projection_side_guard(prop)
        self.assertEqual(prop["best_side"], "UNDER")
        self.assertEqual(prop["best_probability"], 65.0)
        self.assertEqual(prop["best_odds"], -110)
        self.assertAlmostEqual(prop["expected_value_pct"], 24.09, places=1)

    def test_projection_direction_with_negative_ev_is_pass(self):
        prop = {
            "projection": 4.0, "line": 3.5,
            "over_probability": 45.0, "under_probability": 55.0,
            "over_odds": -110, "under_odds": -110,
        }
        _apply_projection_side_guard(prop)
        self.assertEqual(prop["best_side"], "OVER")
        self.assertLess(prop["expected_value_pct"], 0)
        self.assertEqual(prop["recommendation"], "PASS")

    def test_missing_projection_fails_closed(self):
        prop = {"line": 3.5, "best_side": "UNDER", "over_probability": 60, "over_odds": 100}
        _apply_projection_side_guard(prop)
        self.assertIsNone(prop["best_side"])
        self.assertEqual(prop["recommendation"], "PASS")
        self.assertEqual(prop["projection_alignment"], "unavailable")

    def test_projection_review_reports_direction_conflict(self):
        prop = {"projection": 4.0, "line": 3.5, "best_side": "UNDER"}
        self.assertTrue(_projection_side_review(prop))
        self.assertEqual(prop["projection_alignment"], "conflict")


if __name__ == "__main__":
    unittest.main()
