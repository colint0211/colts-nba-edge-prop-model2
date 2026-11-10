import unittest
from scripts.expected_value_calculator import calculate_ev, estimate_market_margin, implied_probability, odds_to_decimal

class ExpectedValueTests(unittest.TestCase):
    def test_american_prices(self):
        self.assertAlmostEqual(odds_to_decimal(-110), 1.9090909091)
        self.assertAlmostEqual(implied_probability(-110), 110 / 210)
        self.assertAlmostEqual(implied_probability(150), 0.4)
    def test_decimal_and_fractional(self):
        self.assertAlmostEqual(odds_to_decimal(1.91, "decimal"), 1.91)
        self.assertAlmostEqual(odds_to_decimal("5/2", "fractional"), 3.5)
    def test_ev_at_55_percent_minus_110(self):
        self.assertAlmostEqual(calculate_ev(0.55, -110).expected_value_per_dollar, 0.05)
    def test_push_probability(self):
        result = calculate_ev(0.52, -110, push_probability=0.02)
        self.assertAlmostEqual(result.expected_value_per_dollar, 0.0127272727)
    def test_two_way_vig(self):
        result = estimate_market_margin([-110, -110])
        self.assertAlmostEqual(result["bookmaker_margin"], 0.0476190476)
        self.assertAlmostEqual(result["side_a_no_vig_probability"], 0.5)
    def test_invalid_probabilities(self):
        with self.assertRaises(ValueError):
            calculate_ev(0.8, -110, push_probability=0.3)

if __name__ == "__main__":
    unittest.main()
