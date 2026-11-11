import unittest

from src.api.game_model import _qualified


class ProvisionalGamePickGateTests(unittest.TestCase):
    def test_advanced_provisional_candidate_can_qualify_and_is_labelled(self):
        row = {
            "probability_calibration_status": "provisional_not_validated",
            "data_quality": "advanced",
            "market_probability": 0.50,
            "model_probability": 0.57,
            "edge_pp": 7.0,
            "ev_pct": 4.0,
        }
        self.assertTrue(_qualified(row))
        self.assertEqual(
            row["selection_status"],
            "PROVISIONAL_MODEL_PICK_NOT_HISTORICALLY_VALIDATED",
        )

    def test_derived_early_season_candidate_has_stricter_threshold(self):
        row = {
            "probability_calibration_status": "provisional_not_validated",
            "data_quality": "derived_early_season",
            "market_probability": 0.50,
            "model_probability": 0.57,
            "edge_pp": 3.9,
            "ev_pct": 4.0,
        }
        self.assertFalse(_qualified(row))
        row["edge_pp"] = 4.5
        self.assertTrue(_qualified(row))

    def test_extreme_derived_gap_and_unknown_quality_are_rejected(self):
        row = {
            "probability_calibration_status": "provisional_not_validated",
            "data_quality": "derived_early_season",
            "market_probability": 0.50,
            "model_probability": 0.70,
            "edge_pp": 13.0,
            "ev_pct": 22.0,
        }
        self.assertFalse(_qualified(row))
        row.update({"data_quality": "unknown", "edge_pp": 8.0, "ev_pct": 5.0})
        self.assertFalse(_qualified(row))


if __name__ == "__main__":
    unittest.main()
