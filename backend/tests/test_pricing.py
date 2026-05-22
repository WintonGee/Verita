"""
Tiered pricing math. Pure-function tests — no DB. Hypothesis covers the
invariants that example-based tests miss.

Default plan (the brief's example):
    Tier 1: [0, 10k)     free
    Tier 2: [10k, 100k)  $0.001/unit = 100_000 micro-cents
    Tier 3: [100k, ∞)    $0.0005/unit = 50_000 micro-cents
"""

import hypothesis.strategies as st
import pytest
from hypothesis import given

from apps.billing.money import MICRO_CENTS_PER_USD, micro_cents_to_usd_str
from apps.billing.pricing import (
    Tier,
    compute_tiered_charge,
    compute_tiered_line_items,
    late_event_adjustment,
)


DEFAULT_TIERS = [
    Tier(0, 10_000, 0, 1),
    Tier(10_000, 100_000, 100_000, 2),
    Tier(100_000, None, 50_000, 3),
]


# --- Boundary cases ----------------------------------------------------------

def test_zero_units_is_free():
    assert compute_tiered_charge(DEFAULT_TIERS, 0) == 0
    assert compute_tiered_line_items(DEFAULT_TIERS, 0) == []


def test_exactly_free_tier_boundary():
    # 10,000 units → all free, no tier-2 charge
    charges = compute_tiered_line_items(DEFAULT_TIERS, 10_000)
    assert len(charges) == 1
    assert charges[0].tier_ordinal == 1
    assert charges[0].units == 10_000
    assert compute_tiered_charge(DEFAULT_TIERS, 10_000) == 0


def test_one_unit_into_tier_two():
    # 10,001 → 10,000 free + 1 @ 100_000 μ¢
    assert compute_tiered_charge(DEFAULT_TIERS, 10_001) == 100_000


def test_exactly_tier_two_boundary():
    # 100,000 → 10k free + 90k @ $0.001 = $90
    total = compute_tiered_charge(DEFAULT_TIERS, 100_000)
    assert total == 90_000 * 100_000  # 9_000_000_000 μ¢
    assert total == 90 * MICRO_CENTS_PER_USD


def test_one_unit_into_tier_three():
    # 100,001 → $90 + 1 @ $0.0005
    total = compute_tiered_charge(DEFAULT_TIERS, 100_001)
    assert total == 90 * MICRO_CENTS_PER_USD + 50_000
    charges = compute_tiered_line_items(DEFAULT_TIERS, 100_001)
    assert [c.tier_ordinal for c in charges] == [1, 2, 3]


def test_deep_into_tier_three():
    # 200,000 → 10k free + 90k@$0.001 + 100k@$0.0005 = $90 + $50 = $140
    total = compute_tiered_charge(DEFAULT_TIERS, 200_000)
    assert total == 140 * MICRO_CENTS_PER_USD


# --- Money formatting --------------------------------------------------------

@pytest.mark.parametrize("mc,expected", [
    (0, "$0"),
    (9_000_000_000, "$90"),
    (50_000, "$0.0005"),
    (100_000, "$0.001"),
    (140 * MICRO_CENTS_PER_USD, "$140"),
    (-1_000_000_000, "-$10"),
])
def test_money_formatting(mc, expected):
    assert micro_cents_to_usd_str(mc) == expected


# --- Property-based invariants -----------------------------------------------

@given(units=st.integers(min_value=0, max_value=10**12))
def test_total_equals_sum_of_line_items(units):
    charges = compute_tiered_line_items(DEFAULT_TIERS, units)
    assert compute_tiered_charge(DEFAULT_TIERS, units) == sum(c.amount_micro_cents for c in charges)


@given(units=st.integers(min_value=0, max_value=10**12))
def test_charge_is_non_negative(units):
    assert compute_tiered_charge(DEFAULT_TIERS, units) >= 0


@given(units=st.integers(min_value=1, max_value=10**12))
def test_charge_is_monotonic(units):
    # Adding a unit never decreases the bill
    assert compute_tiered_charge(DEFAULT_TIERS, units) >= compute_tiered_charge(DEFAULT_TIERS, units - 1)


@given(units=st.integers(min_value=0, max_value=10**9))
def test_line_item_units_sum_to_total(units):
    charges = compute_tiered_line_items(DEFAULT_TIERS, units)
    assert sum(c.units for c in charges) == units


# --- Late-event adjustment ---------------------------------------------------

def test_late_adjustment_uses_marginal_rate():
    # Current usage already deep in tier 3; late units billed at tier-3 rate.
    current = 200_000
    late = 1_000
    adj = late_event_adjustment(DEFAULT_TIERS, current, late)
    assert adj == 1_000 * 50_000  # marginal = tier 3 rate


def test_late_adjustment_spans_tier_boundary():
    # Current at 9,500 (in free tier); 1,000 late units cross into tier 2.
    # 500 of them are still free, 500 at $0.001.
    current = 9_500
    late = 1_000
    adj = late_event_adjustment(DEFAULT_TIERS, current, late)
    assert adj == 500 * 100_000


def test_late_adjustment_zero_units():
    assert late_event_adjustment(DEFAULT_TIERS, 50_000, 0) == 0
