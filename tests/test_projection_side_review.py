import unittest

from src.api.index import _apply_projection_side_guard, _projection_side_review


class ProjectionSideReviewTests(unittest.TestCase):
    def test_projection_above_line_cannot_publish_under(self):
        prop = {"projection": 4.0, "line": 3.5, "best_side": "UNDER"}
        self.assertTrue(_projection_side_review(prop))
        self.assertEqual(prop["projection_alignment"], "conflict")
        self.assertTrue(prop["projection_review_required"])

    def test_projection_below_line_cannot_publish_over(self):
        prop = {"projection": 7.0, "line": 7.5, "best_side": "OVER"}
        self.assertTrue(_projection_side_review(prop))
        self.assertEqual(prop["projection_alignment"], "conflict")

    def test_matching_projection_and_side_passes(self):
        prop = {"projection": 4.0, "line": 3.5, "best_side": "OVER"}
        self.assertFalse(_projection_side_review(prop))
        self.assertEqual(prop["projection_alignment"], "aligned")
        self.assertFalse(prop["projection_review_required"])

    def test_missing_projection_fails_closed(self):
        prop = {"line": 3.5, "best_side": "UNDER"}
        self.assertTrue(_projection_side_review(prop))
        self.assertEqual(prop["projection_alignment"], "unavailable")

    def test_conflicting_under_is_removed_from_board(self):
        prop = {
            "projection": 4.0, "line": 3.5, "best_side": "UNDER",
            "best_odds": 110, "best_probability": 60.0,
            "expected_value_pct": 20.0, "recommendation": "VALUE CANDIDATE",
        }
        _apply_projection_side_guard(prop)
        self.assertIsNone(prop["best_side"])
        self.assertIsNone(prop["expected_value_pct"])
        self.assertEqual(prop["recommendation"], "PASS")
        self.assertEqual(prop["selection_gate_status"], "HOLD")
        self.assertEqual(prop["model_side_before_projection_guard"], "UNDER")

    def test_matching_over_remains_visible(self):
        prop = {
            "projection": 4.0, "line": 3.5, "best_side": "OVER",
            "best_odds": -110, "best_probability": 60.0,
            "expected_value_pct": 20.0, "recommendation": "VALUE CANDIDATE",
        }
        _apply_projection_side_guard(prop)
        self.assertEqual(prop["best_side"], "OVER")
        self.assertEqual(prop["expected_value_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
