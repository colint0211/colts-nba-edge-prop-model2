import unittest

from src.api.index import _projection_side_review


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


if __name__ == "__main__":
    unittest.main()
