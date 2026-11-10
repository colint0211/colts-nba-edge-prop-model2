#!/usr/bin/env python3
"""Expected-value calculator for sportsbook wagers; no external dependencies."""
from __future__ import annotations
import argparse
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

def american_to_decimal(odds: float) -> float:
    if not math.isfinite(odds) or odds == 0:
        raise ValueError("American odds must be finite and non-zero")
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))

def odds_to_decimal(odds: float | str, fmt: str = "american") -> float:
    fmt = fmt.lower()
    if fmt == "american":
        return american_to_decimal(float(odds))
    if fmt == "decimal":
        value = float(odds)
        if not math.isfinite(value) or value <= 1:
            raise ValueError("Decimal odds must be greater than 1")
        return value
    if fmt == "fractional":
        try:
            value = Fraction(str(odds).strip())
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("Fractional odds must look like 5/2") from exc
        if value <= 0:
            raise ValueError("Fractional odds must be positive")
        return 1.0 + float(value)
    raise ValueError("Format must be american, decimal, or fractional")

def implied_probability(odds: float | str, fmt: str = "american") -> float:
    if fmt.lower() == "american":
        a = float(odds)
        if not math.isfinite(a) or a == 0:
            raise ValueError("American odds must be finite and non-zero")
        return 100.0 / (a + 100.0) if a > 0 else abs(a) / (abs(a) + 100.0)
    return 1.0 / odds_to_decimal(odds, fmt)

def estimate_market_margin(two_way_odds: Iterable[float | str], fmt: str = "american") -> dict[str, float]:
    probs = [implied_probability(o, fmt) for o in two_way_odds]
    if len(probs) != 2:
        raise ValueError("Exactly two prices are required for a two-way market")
    total = sum(probs)
    return {"side_a_implied_probability": probs[0], "side_b_implied_probability": probs[1],
            "overround": total, "bookmaker_margin": total - 1.0,
            "side_a_no_vig_probability": probs[0] / total,
            "side_b_no_vig_probability": probs[1] / total}

@dataclass(frozen=True)
class EVResult:
    decimal_odds: float
    break_even_probability: float
    expected_value_per_dollar: float
    expected_return_per_dollar: float
    roi: float

def calculate_ev(model_win_probability: float, odds: float | str, fmt: str = "american",
                 push_probability: float = 0.0) -> EVResult:
    """Probabilities are unconditional: win + push + loss must equal one."""
    win, push = float(model_win_probability), float(push_probability)
    if not (0 <= win <= 1 and 0 <= push <= 1 and win + push <= 1):
        raise ValueError("Win and push probabilities must be in [0,1] and sum to <= 1")
    decimal = odds_to_decimal(odds, fmt)
    loss = 1.0 - win - push
    ev = win * (decimal - 1.0) - loss
    return EVResult(decimal, 1.0 / decimal, ev, 1.0 + ev, ev)

def demo() -> None:
    a = calculate_ev(0.55, -110)
    print("55% model probability at -110")
    print(f"Break-even: {a.break_even_probability:.2%}")
    print(f"EV per $1 risked: \${a.expected_value_per_dollar:+.4f} ({a.roi:+.2%} ROI)")
    b = calculate_ev(0.52, -110, push_probability=0.02)
    print("\\n52% win / 2% push / 46% loss at -110")
    print(f"EV per $1 risked: \${b.expected_value_per_dollar:+.4f}")
    m = estimate_market_margin([-110, -110])
    print("\\nTwo-way market -110 / -110")
    print(f"Overround: {m['bookmaker_margin']:.2%}; no-vig sides: {m['side_a_no_vig_probability']:.2%} / {m['side_b_no_vig_probability']:.2%}")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prob", type=float)
    parser.add_argument("--odds")
    parser.add_argument("--format", choices=("american", "decimal", "fractional"), default="american")
    parser.add_argument("--push", type=float, default=0.0)
    parser.add_argument("--opponent-odds")
    args = parser.parse_args()
    if args.prob is None and args.odds is None:
        demo()
        return
    if args.prob is None or args.odds is None:
        parser.error("--prob and --odds must be supplied together")
    result = calculate_ev(args.prob, args.odds, args.format, args.push)
    print(f"Decimal odds: {result.decimal_odds:.4f}")
    print(f"Break-even probability: {result.break_even_probability:.2%}")
    print(f"EV per $1 risked: \${result.expected_value_per_dollar:+.4f}")
    print(f"Expected ROI: {result.roi:+.2%}")
    if args.opponent_odds is not None:
        margin = estimate_market_margin([args.odds, args.opponent_odds], args.format)
        print(f"Two-way bookmaker margin: {margin['bookmaker_margin']:.2%}")
        print(f"Selected side no-vig probability: {margin['side_a_no_vig_probability']:.2%}")

if __name__ == "__main__":
    main()
