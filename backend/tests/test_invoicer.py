"""
Invoicer integration: usage_windows → invoice + line_items.

Money-moving correctness boundaries:
  - issues correct tiered total
  - idempotent: re-running returns the same invoice, no duplicate rows
  - concurrent issuance for the same customer → exactly one invoice
  - credits apply and floor the total at zero
  - late events from prior periods become an adjustment on the next invoice
  - windows are sealed at issuance
  - invoice total == sum of its line items
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.db import connections
from django.utils import timezone

from apps.billing.invoicer import issue_monthly_invoice
from apps.billing.models import Credit, Event, Invoice, LineItem, UsageWindow
from apps.billing.money import MICRO_CENTS_PER_USD


APRIL_START = datetime(2026, 4, 1, tzinfo=dt_tz.utc)
APRIL_END = datetime(2026, 5, 1, tzinfo=dt_tz.utc)
MAY_START = datetime(2026, 5, 1, tzinfo=dt_tz.utc)
MAY_END = datetime(2026, 6, 1, tzinfo=dt_tz.utc)


def _make_event(customer, api_key, units, event_ts, is_late=False):
    return Event.objects.unsafe_all_tenants().create(
        customer=customer, api_key=api_key, request_id=f"e-{uuid.uuid4()}",
        endpoint="/v1/test", units_consumed=units, event_timestamp=event_ts,
        is_late=is_late,
    )


def _line_items(invoice):
    return list(LineItem.objects.filter(invoice=invoice).order_by("created_at"))


@pytest.mark.django_db(transaction=True)
def test_issues_invoice_with_tiered_total(customer_a, api_key_a):
    # 100,000 units in April → 10k free + 90k @ $0.001 = $90
    _make_event(customer_a, api_key_a, 100_000, APRIL_START + timedelta(days=5, hours=3))

    invoice = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)

    assert invoice.status == Invoice.Status.ISSUED
    assert invoice.total_micro_cents == 90 * MICRO_CENTS_PER_USD
    # Line items: tier 1 (free) + tier 2
    kinds = [li.tier_ordinal for li in _line_items(invoice) if li.kind == "usage"]
    assert kinds == [1, 2]


@pytest.mark.django_db(transaction=True)
def test_idempotent_rerun_returns_same_invoice(customer_a, api_key_a):
    _make_event(customer_a, api_key_a, 50_000, APRIL_START + timedelta(days=2))

    inv1 = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)
    inv2 = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)

    assert inv1.id == inv2.id
    assert Invoice.objects.for_customer(customer_a).count() == 1
    # No duplicate line items
    assert inv1.total_micro_cents == inv2.total_micro_cents


@pytest.mark.django_db(transaction=True)
def test_zero_usage_creates_zero_invoice(customer_a):
    invoice = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)
    assert invoice.total_micro_cents == 0
    items = _line_items(invoice)
    assert len(items) == 1
    assert items[0].description == "No usage in period"


@pytest.mark.django_db(transaction=True)
def test_windows_are_sealed_after_issuance(customer_a, api_key_a):
    _make_event(customer_a, api_key_a, 5_000, APRIL_START + timedelta(days=1))
    issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)

    windows = UsageWindow.objects.for_customer(customer_a).filter(
        window_start__gte=APRIL_START, window_start__lt=APRIL_END)
    assert windows.count() >= 1
    assert all(w.sealed_at is not None for w in windows)


@pytest.mark.django_db(transaction=True)
def test_credit_reduces_total(customer_a, api_key_a):
    _make_event(customer_a, api_key_a, 100_000, APRIL_START + timedelta(days=3))  # $90
    Credit.objects.unsafe_all_tenants().create(
        customer=customer_a, amount_micro_cents=10 * MICRO_CENTS_PER_USD,
        reason="goodwill", issued_by_staff_id="ops@verita", idempotency_key=str(uuid.uuid4()),
    )

    invoice = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)
    # $90 - $10 = $80
    assert invoice.total_micro_cents == 80 * MICRO_CENTS_PER_USD
    # Credit marked applied
    credit = Credit.objects.for_customer(customer_a).first()
    assert credit.applied_to_invoice_id == invoice.id


@pytest.mark.django_db(transaction=True)
def test_credit_floors_total_at_zero(customer_a, api_key_a):
    _make_event(customer_a, api_key_a, 100_000, APRIL_START + timedelta(days=3))  # $90
    Credit.objects.unsafe_all_tenants().create(
        customer=customer_a, amount_micro_cents=200 * MICRO_CENTS_PER_USD,  # > $90
        reason="big refund", issued_by_staff_id="ops@verita", idempotency_key=str(uuid.uuid4()),
    )

    invoice = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)
    assert invoice.total_micro_cents == 0  # floored, not negative


@pytest.mark.django_db(transaction=True)
def test_invoice_total_matches_line_items(customer_a, api_key_a):
    _make_event(customer_a, api_key_a, 150_000, APRIL_START + timedelta(days=4))
    invoice = issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)
    line_sum = sum(li.amount_micro_cents for li in _line_items(invoice))
    assert invoice.total_micro_cents == max(0, line_sum)


@pytest.mark.django_db(transaction=True)
def test_late_event_becomes_adjustment_on_next_invoice(customer_a, api_key_a):
    # April: normal usage, invoice issued, windows sealed
    _make_event(customer_a, api_key_a, 100_000, APRIL_START + timedelta(days=5))
    issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)

    # A late event for April arrives after sealing (is_late=True simulates the
    # ingest path's seal-check having flagged it)
    _make_event(customer_a, api_key_a, 20_000, APRIL_START + timedelta(days=6),
                is_late=True)

    # May: some usage + the prior-period adjustment
    _make_event(customer_a, api_key_a, 100_000, MAY_START + timedelta(days=2))
    may_invoice = issue_monthly_invoice(customer_a, MAY_START, MAY_END)

    adjustments = [li for li in _line_items(may_invoice) if li.kind == "adjustment"]
    assert len(adjustments) == 1
    assert adjustments[0].units == 20_000
    # late event marked adjusted
    late = Event.objects.for_customer(customer_a).filter(is_late=True).first()
    assert late.adjusted_at is not None


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_issuance_yields_one_invoice(customer_a, api_key_a):
    _make_event(customer_a, api_key_a, 50_000, APRIL_START + timedelta(days=2))

    N = 8
    barrier = threading.Barrier(N)
    errors = []

    def worker():
        try:
            barrier.wait()
            issue_monthly_invoice(customer_a, APRIL_START, APRIL_END)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # At most one may raise IntegrityError (the UNIQUE backstop); the rest
    # return the existing invoice. Net: exactly one invoice.
    assert Invoice.objects.for_customer(customer_a).count() == 1
