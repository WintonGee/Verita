"""
Money helpers. All amounts are integer micro-cents (1 unit = $1e-8).
Never use floats for money arithmetic — only for display formatting.
"""

MICRO_CENTS_PER_USD = 100_000_000  # 1e8


def micro_cents_to_usd_str(mc: int) -> str:
    """
    Format micro-cents as a USD string with trailing zeros trimmed.
        9_000_000_000 -> "$90"
        50_000        -> "$0.0005"
        -1_000_000_000 -> "-$10"
    """
    sign = "-" if mc < 0 else ""
    mc = abs(mc)
    dollars = mc // MICRO_CENTS_PER_USD
    frac = mc % MICRO_CENTS_PER_USD
    if frac == 0:
        return f"{sign}${dollars}"
    frac_str = f"{frac:08d}".rstrip("0")
    return f"{sign}${dollars}.{frac_str}"
