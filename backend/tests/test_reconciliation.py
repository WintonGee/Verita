"""
Reconciliation drift detectors. They must catch real drift and not false-alarm
on healthy data.
"""

import uuid
from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.utils import timezone

from apps.billing.aggregator import run_aggregation
from apps.billing.models import Event, Invoice, LineItem, UsageWindow
from apps.billing.money import MICRO_CENTS_PER_USD
from apps.billing.reconciliation import (
    check_invoice_total_drift,
    check_stuck_drafts,
    check_window_drift,
    run_reconciliation,
)


def _make_event(customer, api_key, units, ts):
    return Event.objects.unsafe_all_tenants().create(
        customer=customer, api_key=api_key, request_id=f"e-{uuid.uuid4()}",
        endpoint="/v1/test", units_consumed=units, event_timestamp=ts)


@pytest.mark.django_db(transaction=True)
def test_no_drift_on_healthy_data(customer_a, api_key_a):
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, 10, hour + timedelta(minutes=5))
    run_aggregation(now=timezone.now() + timedelta(minutes=10))

    assert check_window_drift() == []
    assert run_reconciliation()["clean"] is True


@pytest.mark.django_db(transaction=True)
def test_window_drift_detected_when_corrupted(customer_a, api_key_a):
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, 10, hour + timedelta(minutes=5))
    run_aggregation(now=timezone.now() + timedelta(minutes=10))

    # Corrupt the stored window total
    w = UsageWindow.objects.for_customer(customer_a).first()
    w.units_consumed = 999
    w.save()

    drift = check_window_drift()
    assert len(drift) == 1
    assert drift[0]["window_sum"] == 999
    assert drift[0]["event_sum"] == 10
    assert drift[0]["delta"] == -989


@pytest.mark.django_db
def test_invoice_total_drift_detected(customer_a):
    inv = Invoice.objects.create(
        customer=customer_a, period_start=datetime(2026, 4, 1, tzinfo=dt_tz.utc),
        period_end=datetime(2026, 5, 1, tzinfo=dt_tz.utc), status=Invoice.Status.ISSUED,
        total_micro_cents=100 * MICRO_CENTS_PER_USD, currency="USD", issued_at=timezone.now())
    # Line items sum to 90, but stored total says 100 → drift
    LineItem.objects.create(invoice=inv, kind="usage", description="x", units=1,
                            unit_price_micro_cents=0, amount_micro_cents=90 * MICRO_CENTS_PER_USD)

    drift = check_invoice_total_drift()
    assert len(drift) == 1
    assert drift[0]["delta"] == 10 * MICRO_CENTS_PER_USD


@pytest.mark.django_db
def test_stuck_draft_detected(customer_a):
    Invoice.objects.create(
        customer=customer_a, period_start=datetime(2026, 1, 1, tzinfo=dt_tz.utc),
        period_end=datetime(2026, 2, 1, tzinfo=dt_tz.utc), status=Invoice.Status.DRAFT,
        total_micro_cents=0, currency="USD")
    stuck = check_stuck_drafts()
    assert len(stuck) == 1


@pytest.mark.django_db
def test_recent_draft_not_flagged_as_stuck(customer_a):
    now = timezone.now()
    Invoice.objects.create(
        customer=customer_a, period_start=now - timedelta(days=1),
        period_end=now + timedelta(days=1), status=Invoice.Status.DRAFT,
        total_micro_cents=0, currency="USD")
    assert check_stuck_drafts() == []
