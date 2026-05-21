"""
Customer read endpoints: /v1/usage and /v1/invoices.

Boundaries:
  - usage aggregates by bucket, filters by date range + api_key
  - invoices list is tenant-scoped + paginated
  - invoice detail returns line items
  - cross-tenant invoice id → 404 (not 403)
"""

import uuid
from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.test import Client
from django.utils import timezone

from apps.billing.models import Event, Invoice
from apps.billing.money import MICRO_CENTS_PER_USD


@pytest.fixture
def client():
    return Client()


def _auth(api_key):
    return {"HTTP_AUTHORIZATION": f"Bearer {api_key.plaintext_key}"}


def _make_event(customer, api_key, units, ts):
    return Event.objects.unsafe_all_tenants().create(
        customer=customer, api_key=api_key, request_id=f"e-{uuid.uuid4()}",
        endpoint="/v1/test", units_consumed=units, event_timestamp=ts,
    )


# --- Usage -------------------------------------------------------------------

@pytest.mark.django_db
def test_usage_aggregates_by_hour(client, customer_a, api_key_a):
    base = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    _make_event(customer_a, api_key_a, 10, base + timedelta(minutes=5))
    _make_event(customer_a, api_key_a, 20, base + timedelta(minutes=30))
    _make_event(customer_a, api_key_a, 5, base + timedelta(hours=1, minutes=10))

    start = (base - timedelta(hours=1)).isoformat()
    resp = client.get(f"/v1/usage?granularity=hour&start={start}", **_auth(api_key_a))
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Two buckets, newest first
    assert len(data) == 2
    assert data[0]["units_consumed"] == 5
    assert data[1]["units_consumed"] == 30


@pytest.mark.django_db
def test_usage_filters_by_api_key(client, customer_a, api_key_a):
    from tests.factories import ApiKeyFactory
    other_key = ApiKeyFactory(customer=customer_a)
    base = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, 100, base + timedelta(minutes=5))
    _make_event(customer_a, other_key, 7, base + timedelta(minutes=10))

    start = (base - timedelta(hours=1)).isoformat()
    resp = client.get(
        f"/v1/usage?granularity=hour&start={start}&api_key_id={other_key.id}",
        **_auth(api_key_a))
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["units_consumed"] == 7


@pytest.mark.django_db
def test_usage_only_returns_own_tenant(client, customer_a, api_key_a, customer_b, api_key_b):
    base = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, 50, base + timedelta(minutes=5))
    _make_event(customer_b, api_key_b, 999, base + timedelta(minutes=5))

    start = (base - timedelta(hours=1)).isoformat()
    resp = client.get(f"/v1/usage?start={start}", **_auth(api_key_a))
    total = sum(d["units_consumed"] for d in resp.json()["data"])
    assert total == 50  # customer_b's 999 not visible


@pytest.mark.django_db
def test_usage_cursor_pagination(client, customer_a, api_key_a):
    base = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=10)
    for h in range(6):
        _make_event(customer_a, api_key_a, 10, base + timedelta(hours=h, minutes=1))

    start = (base - timedelta(hours=1)).isoformat()
    page1 = client.get(f"/v1/usage?granularity=hour&start={start}&limit=3",
                       **_auth(api_key_a)).json()
    assert len(page1["data"]) == 3
    assert page1["next_cursor"] is not None

    page2 = client.get(
        f"/v1/usage?granularity=hour&start={start}&limit=3&cursor={page1['next_cursor']}",
        **_auth(api_key_a)).json()
    assert len(page2["data"]) == 3
    # No overlap between pages
    p1_buckets = {d["window_start"] for d in page1["data"]}
    p2_buckets = {d["window_start"] for d in page2["data"]}
    assert p1_buckets.isdisjoint(p2_buckets)


# --- Invoices ----------------------------------------------------------------

def _make_invoice(customer, period_start, total):
    return Invoice.objects.create(
        customer=customer,
        period_start=period_start,
        period_end=period_start + timedelta(days=30),
        status=Invoice.Status.ISSUED,
        total_micro_cents=total,
        currency="USD",
        issued_at=timezone.now(),
    )


@pytest.mark.django_db
def test_invoice_list_is_tenant_scoped(client, customer_a, api_key_a, customer_b, api_key_b):
    _make_invoice(customer_a, datetime(2026, 3, 1, tzinfo=dt_tz.utc), 10 * MICRO_CENTS_PER_USD)
    _make_invoice(customer_a, datetime(2026, 4, 1, tzinfo=dt_tz.utc), 20 * MICRO_CENTS_PER_USD)
    _make_invoice(customer_b, datetime(2026, 4, 1, tzinfo=dt_tz.utc), 99 * MICRO_CENTS_PER_USD)

    resp = client.get("/v1/invoices", **_auth(api_key_a))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2  # only customer_a's
    assert all("id" in row for row in body["data"])


@pytest.mark.django_db
def test_invoice_detail_returns_line_items(client, customer_a, api_key_a):
    from apps.billing.models import LineItem
    inv = _make_invoice(customer_a, datetime(2026, 4, 1, tzinfo=dt_tz.utc), 90 * MICRO_CENTS_PER_USD)
    LineItem.objects.create(invoice=inv, kind="usage", description="Tier 2",
                            units=90000, unit_price_micro_cents=100_000,
                            amount_micro_cents=90 * MICRO_CENTS_PER_USD, tier_ordinal=2)

    resp = client.get(f"/v1/invoices/{inv.id}", **_auth(api_key_a))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["line_items"]) == 1
    assert body["line_items"][0]["tier_ordinal"] == 2


@pytest.mark.django_db
def test_invoice_detail_cross_tenant_returns_404(client, customer_a, api_key_a, customer_b):
    other = _make_invoice(customer_b, datetime(2026, 4, 1, tzinfo=dt_tz.utc), 50 * MICRO_CENTS_PER_USD)
    resp = client.get(f"/v1/invoices/{other.id}", **_auth(api_key_a))
    assert resp.status_code == 404  # not 403 — existence not confirmed


@pytest.mark.django_db
def test_invoice_detail_nonexistent_returns_404(client, customer_a, api_key_a):
    resp = client.get(f"/v1/invoices/{uuid.uuid4()}", **_auth(api_key_a))
    assert resp.status_code == 404
