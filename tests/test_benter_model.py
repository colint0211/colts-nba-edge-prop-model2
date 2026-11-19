"""Unit tests for the isolated Benter NBA probability engine."""
import math
import random
import unittest

from src.api.benter_model import (
    combine_benter_probability,
    expected_value_units,
    fit_benter_combiner,
    fit_logistic_regression,
    no_vig_probabilities,
    predict_logistic,
    probability_metrics,
    walk_forward_benter,
)


class BenterModelTests(unittest.TestCase):
    def test_no_vig_removes_symmetric_hold(self):
        result = no_vig_probabilities(-110, -110)
        self.assertAlmostEqual(result["over"], 0.5, places=10)
        self.assertAlmostEqual(result["under"], 0.5, places=10)
        self.assertGreater(result["hold"], 0)

    def test_expected_value_respects_price(self):
        self.assertAlmostEqual(expected_value_units(0.5, 0, 100), 0.0)
        self.assertLess(expected_value_units(0.5, 0, -110), 0.0)

    def test_logistic_model_learns_signal(self):
        features = [[float(i % 7), float(i % 3)] for i in range(300)]
        targets = [int((row[0] + row[1] * 0.5) > 3.2) for row in features]
        model = fit_logistic_regression(features, targets, l2=0.1)
        probs = predict_logistic(model, [[6, 2], [0, 0]])
        self.assertGreater(probs[0], probs[1])

    def test_benter_combiner_learns_market_and_fundamental(self):
        rng = random.Random(42)
        fund, market, target = [], [], []
        for _ in range(1200):
            f = 0.2 + rng.random() * 0.6
            m = 0.2 + rng.random() * 0.6
            latent = 0.72 * math.log(f / (1 - f)) + 0.55 * math.log(m / (1 - m))
            p = 1 / (1 + math.exp(-latent))
            y = int(rng.random() < p)
            fund.append(f)
            market.append(m)
            target.append(y)
        model = fit_benter_combiner(fund, market, target)
        combined = combine_benter_probability(model, 0.75, 0.55)
        self.assertGreater(combined, 0.5)
        self.assertEqual(model["input_order"], ["fundamental_logit", "market_logit"])

    def test_walk_forward_is_date_grouped_and_requires_both_stages(self):
        rng = random.Random(7)
        rows = []
        for day in range(100):
            date = f"2026-10-{(day % 28) + 1:02d}" if day < 28 else f"2026-{11 + (day // 28):02d}-{(day % 28) + 1:02d}"
            for _ in range(3):
                x = rng.uniform(-2, 2)
                market = min(0.85, max(0.15, 0.5 + 0.12 * x + rng.uniform(-0.12, 0.12)))
                p = 1 / (1 + math.exp(-(0.9 * x + 0.35 * math.log(market / (1 - market)))))
                y = int(rng.random() < p)
                rows.append({"date": date, "features": [x], "market_probability": market, "target": y})

        result = walk_forward_benter(
            rows, minimum_stage1_rows=30, minimum_stage2_rows=30
        )
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["predictions"])
        for prediction in result["predictions"]:
            self.assertGreaterEqual(prediction["stage1_training_rows"], 30)
            self.assertGreaterEqual(prediction["stage2_training_oos_rows"], 30)
        first_prediction_date = result["predictions"][0]["date"]
        earlier = [r for r in rows if r["date"] < first_prediction_date]
        self.assertGreaterEqual(len(earlier), 30)
        metrics = probability_metrics(result["predictions"])
        self.assertGreater(metrics["n"], 0)
        self.assertGreaterEqual(metrics["brier"], 0.0)

    def test_walk_forward_rejects_push_target(self):
        with self.assertRaises(ValueError):
            walk_forward_benter([{
                "date": "2026-11-01",
                "features": [1.0],
                "market_probability": 0.5,
                "target": 2,
            }])


if __name__ == "__main__":
    unittest.main()
