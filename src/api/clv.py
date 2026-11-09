"""Pure helpers for evaluating market movement on locked NBA wagers."""

def signed_line_clv(market, selection, bet_line, observed_line):
    """Positive means the observed line moved in the bettor's favor."""
    bet_line = float(bet_line)
    observed_line = float(observed_line)
    if market == "sides":
        return observed_line - bet_line
    if market == "totals":
        return (observed_line - bet_line) if str(selection).upper() == "OVER" else (bet_line - observed_line)
    raise ValueError("market must be 'sides' or 'totals'")


def summarize_clv(rows):
    """Summarize only matched, observed wagers; never impute missing quotes."""
    grouped = {}
    for row in rows:
        market = row["market"]
        grouped.setdefault(market, []).append(row)

    def pack(items):
        if not items:
            return {"matched": 0, "avg_line_clv_points": None, "positive_line_clv_pct": None,
                    "avg_price_clv_pp": None, "positive_price_clv_pct": None,
                    "avg_minutes_before_tip": None}
        line_values = [float(x["line_clv_points"]) for x in items]
        price_values = [float(x["price_clv_pp"]) for x in items if x.get("price_clv_pp") is not None]
        gaps = [float(x["minutes_before_tip"]) for x in items if x.get("minutes_before_tip") is not None]
        return {
            "matched": len(items),
            "avg_line_clv_points": round(sum(line_values) / len(line_values), 3),
            "positive_line_clv_pct": round(100 * sum(x > 0 for x in line_values) / len(line_values), 1),
            "avg_price_clv_pp": round(sum(price_values) / len(price_values), 3) if price_values else None,
            "positive_price_clv_pct": round(100 * sum(x > 0 for x in price_values) / len(price_values), 1) if price_values else None,
            "avg_minutes_before_tip": round(sum(gaps) / len(gaps), 1) if gaps else None,
        }

    return {"matched": len(rows), "by_market": {market: pack(items) for market, items in grouped.items()},
            "all_markets": pack(rows)}
