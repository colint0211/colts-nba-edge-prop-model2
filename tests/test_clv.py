import unittest

from src.api.clv import signed_line_clv, summarize_clv


class ClosingLineValueTests(unittest.TestCase):
    def test_spread_line_move_toward_bet_is_positive(self):
        self.assertEqual(signed_line_clv("sides", "BOS", -3.5, -2.5), 1.0)

    def test_over_benefits_from_higher_total(self):
        self.assertEqual(signed_line_clv("totals", "OVER", 220.5, 222.0), 1.5)

    def test_under_benefits_from_lower_total(self):
        self.assertEqual(signed_line_clv("totals", "UNDER", 220.5, 218.5), 2.0)

    def test_missing_clv_is_not_counted(self):
        result = summarize_clv([{"market": "totals", "line_clv_points": 1.5,
                                 "price_clv_pp": 0.8, "minutes_before_tip": 12}])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["by_market"]["totals"]["positive_line_clv_pct"], 100.0)
        self.assertEqual(result["all_markets"]["avg_price_clv_pp"], 0.8)


if __name__ == "__main__":
    unittest.main()
