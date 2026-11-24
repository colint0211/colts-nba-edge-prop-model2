import json
import unittest
from unittest.mock import patch

from src.api import nba_totals


class NbaTotalsTests(unittest.TestCase):
    def test_parser_needs_only_draftkings_full_game_totals(self):
        events = [{
            "id": "nba-game-1",
            "commence_time": "2026-11-24T00:00:00Z",
            "away_team": "Boston Celtics",
            "home_team": "New York Knicks",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "totals",
                    "line_type": "main",
                    "period": "full_game",
                    "last_update": "2026-11-23T17:00:00Z",
                    "outcomes": [
                        {"name": "Over", "point": 222.5, "price": -110},
                        {"name": "Under", "point": 222.5, "price": -110},
                    ],
                }],
            }],
        }]
        rows = nba_totals._parse_total_lines(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["totals"]["over"]["line"], 222.5)
        self.assertEqual(rows[0]["totals"]["under"]["odds"], -110)

    def test_parser_rejects_alternate_total(self):
        events = [{
            "id": "nba-game-2", "commence_time": "2026-11-24T00:00:00Z",
            "away_team": "Boston Celtics", "home_team": "New York Knicks",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "totals", "line_type": "alternate",
                    "outcomes": [
                        {"name": "Over", "point": 230.5, "price": 120},
                        {"name": "Under", "point": 230.5, "price": -140},
                    ],
                }],
            }],
        }]
        self.assertEqual(nba_totals._parse_total_lines(events), [])

    def test_integer_total_has_explicit_push_probability(self):
        result = nba_totals._normal_probabilities(225.0, 17.0, 225.0)
        self.assertGreater(result["push"], 0.0)
        self.assertAlmostEqual(result["over"] + result["under"], 1.0, places=10)
        self.assertAlmostEqual(result["over_win"] + result["under_win"] + result["push"], 1.0, places=10)

    def test_half_point_total_has_no_push_probability(self):
        result = nba_totals._normal_probabilities(225.0, 17.0, 224.5)
        self.assertEqual(result["push"], 0.0)
        self.assertAlmostEqual(result["over"] + result["under"], 1.0, places=10)

    def test_minimum_price_targets_three_percent_expected_value(self):
        price = nba_totals._minimum_odds(0.5, 0.5, 0.03)
        payout = price / 100.0 if price > 0 else 100.0 / abs(price)
        expected_value = 0.5 * payout - 0.5
        self.assertAlmostEqual(expected_value, 0.03, delta=0.005)

    def test_fair_odds_are_independent_of_market_prices(self):
        self.assertEqual(nba_totals._american_odds(0.5), -100)
        self.assertEqual(nba_totals._american_odds(0.4), 150)

    def test_paid_prop_line_fetch_fails_closed_under_free_only_policy(self):
        with self.assertRaisesRegex(RuntimeError, "Free-only policy"):
            nba_totals._fetch_lines(force=True)

    def test_holdout_pass_does_not_enable_official_totals_without_market_validation(self):
        payload = {
            "calibration_fit_on_train_only": {"total_offset_points": -0.1},
            "train_raw": {"games": 600, "total_residual_sd": 19.0},
            "holdout_raw": {"total_mae": 15.0},
            "holdout_after_train_calibration": {
                "games": 120, "total_bias": 1.5, "total_mae": 14.9
            },
            "holdout_probability_calibration": {
                "brier": 0.20, "mean_absolute_calibration_error": 0.04
            },
        }
        with patch.object(nba_totals.Path, "read_text", return_value=json.dumps(payload)):
            result = nba_totals._load_calibration()
        self.assertTrue(result["holdout_gate_passed"])
        self.assertFalse(result["market_relative_validated"])
        self.assertFalse(result["validated"])
        self.assertEqual(result["status"], "MARKET_RELATIVE_VALIDATION_REQUIRED")

    def test_explicit_market_relative_pass_is_required_in_addition_to_holdout(self):
        payload = {
            "calibration_fit_on_train_only": {"total_offset_points": -0.1},
            "train_raw": {"games": 600, "total_residual_sd": 19.0},
            "holdout_raw": {"total_mae": 15.0},
            "holdout_after_train_calibration": {
                "games": 120, "total_bias": 1.5, "total_mae": 14.9
            },
            "holdout_probability_calibration": {
                "brier": 0.20, "mean_absolute_calibration_error": 0.04
            },
            "market_relative_validation": {"passed": True},
        }
        with patch.object(nba_totals.Path, "read_text", return_value=json.dumps(payload)):
            result = nba_totals._load_calibration()
        self.assertTrue(result["holdout_gate_passed"])
        self.assertTrue(result["market_relative_validated"])
        self.assertTrue(result["validated"])


if __name__ == "__main__":
    unittest.main()
