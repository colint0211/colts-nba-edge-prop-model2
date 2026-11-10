# Expected Value Calculator

Standalone research utility. It does not change the frozen away-only ATS candidate, generate picks, or prove that a positive-EV estimate is calibrated.

## Features
- American, decimal, and fractional odds.
- Raw break-even probability from the offered price (includes vig).
- Two-way bookmaker overround and proportional no-vig probabilities.
- Expected net profit and ROI per $1 risked, including pushes.

## Push-aware calculation
Let W, P, and L be unconditional model probabilities of win, push, and loss, with W + P + L = 1. Let D be decimal odds. For a $1 stake:

EV = W × (D − 1) − L

A push returns the stake and contributes zero net profit. Expected gross return including returned stake is 1 + EV. The calculator expects unconditional probabilities; do not pass a win probability conditional on no push.

## Worked examples

### 1. -110, model win probability 55%, no push
- Decimal odds: 1.9091
- Break-even probability: 52.38%
- EV: 0.55 × 0.9091 − 0.45 = +$0.0500 per $1 risked
- Expected ROI: +5.00%

### 2. -110, 52% win / 2% push / 46% loss
- EV: 0.52 × 0.9091 − 0.46 = +$0.0127 per $1 risked
- Push returns the stake; it is neither a win nor a loss.

### 3. Two-way market, -110 / -110
- Raw implied probability on each side: 52.38%
- Overround/bookmaker margin: 4.76 percentage points
- Proportional no-vig probability: 50% / 50%

Overround is the sum of raw implied probabilities minus one. It estimates the quoted market's margin, not necessarily realized bookmaker profit. Proportional de-vigging is one convention; alternatives exist.

## Run
```bash
python scripts/expected_value_calculator.py
python scripts/expected_value_calculator.py --prob 0.55 --odds -110
python scripts/expected_value_calculator.py --prob 0.52 --push 0.02 --odds -110 --opponent-odds -110
python scripts/expected_value_calculator.py --prob 0.55 --odds 1.91 --format decimal
python scripts/expected_value_calculator.py --prob 0.55 --odds 5/6 --format fractional
python -m unittest tests.test_expected_value_calculator -v
```

## Safeguards
- EV quality depends on calibrated model probabilities; a biased probability creates misleading EV.
- Use the price actually available at decision time. Do not backfill closing prices into an earlier decision.
- Calculate payout EV from the offered price. Use no-vig probabilities for market comparison, not as a replacement for the actual payout.
- Estimate margin only from both sides at the same book and timestamp.
- Keep full precision internally; round only for display.
- Track realized ROI, CLV, Brier score, log loss, and calibration separately. EV is a forecast, not proof of an edge.
- No stake-sizing or bet recommendation is included.
