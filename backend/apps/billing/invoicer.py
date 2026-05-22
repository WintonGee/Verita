"""
Monthly invoicer: usage_windows → invoice + line_items.

Per-customer, fully transactional, idempotent. Sequence (see PIPELINE.md §3):

  1. per-customer advisory xact locks: the billing lock (serializes against the
     aggregator + other invoicer runs) and the EXCLUSIVE seal lock (excludes
     concurrent ingest, which holds it in SHARED mode — see views_v1.py)
  2. idempotency check — if an invoice already exists for (customer, period),
     return it (no-op)
  3. re-aggregate the period (idempotent UPSERT) so windows reflect every
     committed event before we seal
  4. create the draft invoice
  5. read+lock windows, sum units, compute tiered line items
  6. late-event adjustment for events from PRIOR periods (is_late + unadjusted)
  7. apply pending credits
  8. roll up total, mark issued
  9. seal the period's windows
  10. write audit row

Idempotency rests on (a) the advisory locks, (b) the existence check, and
(c) the UNIQUE(customer, period_start) constraint as a hard backstop.

Seal-race correctness: because ingest holds the seal lock SHARED and we hold it
EXCLUSIVE, no ingest can interleave with steps 3-9. An event therefore either
commits before our exclusive lock (so step 3 re-aggregates it onto this invoice)
or after we release (so its ingest sees this issued invoice and self-flags
is_late, to be adjusted next period). No event is lost or double-billed; no
wall-clock sweep is needed.
"""

import logging
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import write_audit
from apps.billing.models import Credit, Event, Invoice, LineItem, UsageWindow
from apps.billing.money import micro_cents_to_usd_str
from apps.billing.pricing import (
    compute_tiered_line_items,
    late_event_adjustment,
    tiers_from_plan,
)

logger = logging.getLogger("verita.invoicer")


def previous_month_period(now):
    """(period_start, period_end) for the calendar month before `now`, UTC."""
    first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_prev = first_of_this - timedelta(days=1)
    period_start = last_of_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return period_start, first_of_this


def month_period_containing(dt):
    """(period_start, period_end) for the calendar month containing dt, UTC."""
    period_start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # advance one month
    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1)
    return period_start, period_end


def _reaggregate_period(cur, customer_id, period_start, period_end):
    """Final idempotent UPSERT so windows reflect all committed events."""
    cur.execute(
        """
        INSERT INTO usage_window (
            id, customer_id, window_start,
            units_consumed, event_count, last_recomputed_at
        )
        SELECT
            gen_random_uuid(), customer_id,
            date_trunc('hour', event_timestamp),
            SUM(units_consumed), COUNT(*), NOW()
          FROM event
         WHERE customer_id = %s::uuid
           AND event_timestamp >= %s AND event_timestamp < %s
         GROUP BY customer_id, date_trunc('hour', event_timestamp)
        ON CONFLICT (customer_id, window_start)
        DO UPDATE SET
            units_consumed = EXCLUDED.units_consumed,
            event_count = EXCLUDED.event_count,
            last_recomputed_at = NOW()
          WHERE usage_window.sealed_at IS NULL
        """,
        [str(customer_id), period_start, period_end],
    )


def issue_monthly_invoice(customer, period_start, period_end, now=None):
    """
    Issue (or return the existing) invoice for one customer + period.
    Returns the Invoice instance.
    """
    now = now or timezone.now()

    with transaction.atomic():
        with connection.cursor() as cur:
            # Billing lock: serialize against the aggregator + other invoicer
            # runs for this customer.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [f"verita:billing:{customer.id}"],
            )
            # Seal lock (EXCLUSIVE): ingest holds this SHARED, so taking it
            # exclusive drains in-flight ingests and blocks new ones until we
            # commit — closing the seal race by construction.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [f"verita:seal:{customer.id}"],
            )

            # 2. Idempotency: already issued for this period?
            existing = (Invoice.objects.for_customer(customer)
                        .filter(period_start=period_start).first())
            if existing:
                return existing

            # 3. Re-aggregate so windows are current before we seal.
            _reaggregate_period(cur, customer.id, period_start, period_end)

        # 4. Draft invoice
        invoice = Invoice.objects.create(
            customer=customer,
            period_start=period_start,
            period_end=period_end,
            status=Invoice.Status.DRAFT,
            currency=customer.price_plan.currency,
            total_micro_cents=0,
        )

        # 5. Sum windows for the period (lock them; we hold the advisory lock
        #    anyway, this is belt-and-suspenders).
        windows = (UsageWindow.objects.for_customer(customer)
                   .filter(window_start__gte=period_start, window_start__lt=period_end)
                   .select_for_update())
        total_units = windows.aggregate(s=Sum("units_consumed"))["s"] or 0

        tiers = tiers_from_plan(customer.price_plan)
        line_items = []

        usage_charges = compute_tiered_line_items(tiers, total_units)
        for ch in usage_charges:
            line_items.append(LineItem(
                invoice=invoice,
                kind=LineItem.Kind.USAGE,
                description=ch.description,
                units=ch.units,
                unit_price_micro_cents=ch.unit_price_micro_cents,
                amount_micro_cents=ch.amount_micro_cents,
                tier_ordinal=ch.tier_ordinal,
            ))

        if not usage_charges:
            line_items.append(LineItem(
                invoice=invoice,
                kind=LineItem.Kind.USAGE,
                description="No usage in period",
                units=0,
                unit_price_micro_cents=0,
                amount_micro_cents=0,
            ))

        # 6. Late events from PRIOR periods → adjustment line item.
        late_qs = (Event.objects.for_customer(customer)
                   .filter(is_late=True, adjusted_at__isnull=True,
                           event_timestamp__lt=period_start)
                   .select_for_update())
        late_events = list(late_qs)
        if late_events:
            late_units = sum(e.units_consumed for e in late_events)
            adj_amount = late_event_adjustment(tiers, total_units, late_units)
            line_items.append(LineItem(
                invoice=invoice,
                kind=LineItem.Kind.ADJUSTMENT,
                description=(f"Adjustment: {late_units:,} late units from prior "
                            f"periods @ marginal rate = {micro_cents_to_usd_str(adj_amount)}"),
                units=late_units,
                unit_price_micro_cents=0,
                amount_micro_cents=adj_amount,
            ))

        # 7. Apply pending credits (negative line items).
        credits = list((Credit.objects.for_customer(customer)
                        .filter(applied_to_invoice__isnull=True)
                        .select_for_update()))
        for credit in credits:
            line_items.append(LineItem(
                invoice=invoice,
                kind=LineItem.Kind.CREDIT_APPLICATION,
                description=f"Credit applied: {credit.reason}",
                units=0,
                unit_price_micro_cents=0,
                amount_micro_cents=-credit.amount_micro_cents,
            ))

        LineItem.objects.bulk_create(line_items)

        # 8. Roll up total (floor at zero — credits can't make a negative bill).
        raw_total = sum(li.amount_micro_cents for li in line_items)
        invoice.total_micro_cents = max(0, raw_total)
        invoice.status = Invoice.Status.ISSUED
        invoice.issued_at = now
        invoice.save(update_fields=["total_micro_cents", "status", "issued_at"])

        # Mark consumed records now that the invoice is issued.
        if late_events:
            (Event.objects.for_customer(customer)
             .filter(id__in=[e.id for e in late_events])
             .update(adjusted_at=now))
        if credits:
            (Credit.objects.for_customer(customer)
             .filter(id__in=[c.id for c in credits])
             .update(applied_to_invoice=invoice))

        # 9. Seal the period's windows (idempotent: WHERE sealed_at IS NULL).
        #    Safe under the exclusive seal lock: no ingest can add a window to
        #    this period concurrently. Events arriving after we commit see the
        #    issued invoice and self-flag is_late at ingest (no sweep needed).
        (UsageWindow.objects.for_customer(customer)
         .filter(window_start__gte=period_start, window_start__lt=period_end,
                 sealed_at__isnull=True)
         .update(sealed_at=now))

        # 10. Audit
        write_audit(
            actor_type="system",
            actor_id="invoicer",
            action="invoice.issue",
            resource_type="invoice",
            resource_id=invoice.id,
            after={
                "total_micro_cents": invoice.total_micro_cents,
                "total_units": int(total_units),
                "line_items": len(line_items),
                "period_start": period_start.isoformat(),
                "credits_applied": len(credits),
                "late_events_adjusted": len(late_events),
            },
        )

        logger.info("issued invoice %s for customer %s: %s",
                    invoice.id, customer.id,
                    micro_cents_to_usd_str(invoice.total_micro_cents))
        return invoice


def run_invoicing(period_start=None, period_end=None, now=None, customer_ids=None):
    """
    Issue invoices for all (or the given) customers for a period.
    Defaults to the previous calendar month.
    """
    from apps.tenancy.models import Customer

    now = now or timezone.now()
    if period_start is None or period_end is None:
        period_start, period_end = previous_month_period(now)

    customers = Customer.objects.all()
    if customer_ids:
        customers = customers.filter(id__in=customer_ids)

    issued = 0
    for customer in customers.iterator():
        issue_monthly_invoice(customer, period_start, period_end, now=now)
        issued += 1
    logger.info("invoicing complete: %d customers, period %s–%s",
                issued, period_start.date(), period_end.date())
    return {"customers": issued, "period_start": period_start, "period_end": period_end}
