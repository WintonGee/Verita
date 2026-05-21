"""
Reconciliation: read-only drift detectors. They find disagreements between the
source of truth and the denormalized rollups, and return structured findings
(in production these would emit metrics / alerts). They never mutate state.

This is the machinery behind "how ops debugs a wrong invoice" (DESIGN.md §3):
walk the same three comparisons to localize where a discrepancy entered.
"""

from datetime import timedelta

from django.db import connection
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import Invoice, LineItem


def check_window_drift(since_days=7) -> list[dict]:
    """
    Windows whose stored units_consumed disagrees with the live SUM of their
    events. A nonzero result on a SEALED window is a paging-level invariant
    violation; on an unsealed window it's usually a transient mid-aggregation
    state.
    """
    since = timezone.now() - timedelta(days=since_days)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT e.customer_id,
                   date_trunc('hour', e.event_timestamp) AS window_start,
                   SUM(e.units_consumed) AS event_sum,
                   uw.units_consumed AS window_sum,
                   uw.sealed_at IS NOT NULL AS sealed
              FROM event e
              JOIN usage_window uw
                ON uw.customer_id = e.customer_id
               AND uw.window_start = date_trunc('hour', e.event_timestamp)
             WHERE e.event_timestamp >= %s
             GROUP BY e.customer_id, date_trunc('hour', e.event_timestamp),
                      uw.units_consumed, uw.sealed_at
            HAVING SUM(e.units_consumed) <> uw.units_consumed
            """,
            [since],
        )
        rows = cur.fetchall()
    return [
        {
            "customer_id": str(r[0]),
            "window_start": r[1].isoformat(),
            "event_sum": int(r[2]),
            "window_sum": int(r[3]),
            "delta": int(r[2]) - int(r[3]),
            "sealed": r[4],
            "severity": "page" if r[4] else "warn",
        }
        for r in rows
    ]


def check_invoice_total_drift() -> list[dict]:
    """Issued/paid invoices whose total disagrees with the sum of their line items."""
    findings = []
    invoices = Invoice.objects.unsafe_all_tenants().filter(
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PAID])
    for inv in invoices.iterator():
        line_sum = (LineItem.objects.filter(invoice=inv)
                    .aggregate(s=Sum("amount_micro_cents"))["s"] or 0)
        expected = max(0, line_sum)
        if inv.total_micro_cents != expected:
            findings.append({
                "invoice_id": str(inv.id),
                "customer_id": str(inv.customer_id),
                "stored_total": inv.total_micro_cents,
                "line_item_sum": expected,
                "delta": inv.total_micro_cents - expected,
                "severity": "page",
            })
    return findings


def check_stuck_drafts(grace_days=2) -> list[dict]:
    """Invoices still in draft well after their period ended — the invoicer cron
    likely failed for them."""
    cutoff = timezone.now() - timedelta(days=grace_days)
    stuck = Invoice.objects.unsafe_all_tenants().filter(
        status=Invoice.Status.DRAFT, period_end__lt=cutoff)
    return [
        {"invoice_id": str(i.id), "customer_id": str(i.customer_id),
         "period_end": i.period_end.isoformat(), "severity": "warn"}
        for i in stuck
    ]


def run_reconciliation() -> dict:
    window = check_window_drift()
    invoice = check_invoice_total_drift()
    drafts = check_stuck_drafts()
    return {
        "window_drift": window,
        "invoice_total_drift": invoice,
        "stuck_drafts": drafts,
        "clean": not (window or invoice or drafts),
    }
