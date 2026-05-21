"""
Tiered pricing engine. Pure functions over plain tier tuples — no DB, no
side effects — so they're trivially unit- and property-testable.

A tier is (start_unit, end_unit_or_None, unit_price_micro_cents, ordinality),
covering [start_unit, end_unit). end_unit None means infinity. Tiers in a plan
must cover [0, ∞) without gaps (validated at plan creation, not here).

The brief's example plan:
    0–10k        free
    10k–100k     $0.001/unit  (100_000 micro-cents)
    100k+        $0.0005/unit ( 50_000 micro-cents)
"""

from dataclasses import dataclass

from apps.billing.money import micro_cents_to_usd_str


@dataclass(frozen=True)
class Tier:
    start_unit: int
    end_unit: int | None
    unit_price_micro_cents: int
    ordinality: int


@dataclass(frozen=True)
class TierCharge:
    tier_ordinal: int
    units: int
    unit_price_micro_cents: int
    amount_micro_cents: int
    description: str


def _describe(tier: Tier, units: int) -> str:
    upper = "∞" if tier.end_unit is None else f"{tier.end_unit:,}"
    rate = micro_cents_to_usd_str(tier.unit_price_micro_cents)
    band = f"{tier.start_unit:,}–{upper}"
    if tier.unit_price_micro_cents == 0:
        return f"Tier {tier.ordinality}: {units:,} units in {band} (free)"
    return f"Tier {tier.ordinality}: {units:,} units in {band} @ {rate}/unit"


def compute_tiered_line_items(tiers: list[Tier], total_units: int) -> list[TierCharge]:
    """
    Split total_units across the tiers and return one TierCharge per tier
    that has any units. Tiers must be sorted by ordinality / start_unit.
    """
    if total_units < 0:
        raise ValueError("total_units must be non-negative")

    charges: list[TierCharge] = []
    for tier in sorted(tiers, key=lambda t: t.start_unit):
        if total_units <= tier.start_unit:
            break  # usage doesn't reach this tier
        tier_upper = tier.end_unit if tier.end_unit is not None else total_units
        units_in_tier = min(total_units, tier_upper) - tier.start_unit
        if units_in_tier <= 0:
            continue
        amount = units_in_tier * tier.unit_price_micro_cents
        charges.append(TierCharge(
            tier_ordinal=tier.ordinality,
            units=units_in_tier,
            unit_price_micro_cents=tier.unit_price_micro_cents,
            amount_micro_cents=amount,
            description=_describe(tier, units_in_tier),
        ))
    return charges


def compute_tiered_charge(tiers: list[Tier], total_units: int) -> int:
    """Total micro-cents for total_units across the tiers."""
    return sum(c.amount_micro_cents for c in compute_tiered_line_items(tiers, total_units))


def late_event_adjustment(tiers: list[Tier], current_total_units: int,
                          late_units: int) -> int:
    """
    Marginal cost of adding `late_units` on top of the current period's usage.

        adjustment = charge(current_total + late) - charge(current_total)

    This applies the customer's current marginal tier rate to the late units.
    It is a documented simplification (DESIGN.md §6): exact billing of late
    events at their *original* period's rate would require plan versioning and
    per-period tier recomputation, which is out of scope. Using the current
    marginal rate is deterministic and errs neither systematically high nor low.
    """
    if late_units <= 0:
        return 0
    return (compute_tiered_charge(tiers, current_total_units + late_units)
            - compute_tiered_charge(tiers, current_total_units))


def tiers_from_plan(price_plan) -> list[Tier]:
    """Adapter: PricePlan ORM instance -> list[Tier]."""
    return [
        Tier(
            start_unit=t.start_unit,
            end_unit=t.end_unit,
            unit_price_micro_cents=t.unit_price_micro_cents,
            ordinality=t.ordinality,
        )
        for t in price_plan.tiers.all()
    ]
