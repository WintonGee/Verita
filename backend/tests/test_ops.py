"""
Ops console endpoints. Correctness boundaries:
  - credit issuance is idempotent (double-click → one credit)
  - reused Idempotency-Key with a different body → 409
  - every money-moving action writes an audit row (immutable)
  - line-item override recomputes the invoice total + audits
  - override disallowed on paid invoices
  - auth isolation: customer creds can't reach /ops; staff perms required
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.billing.models import Credit, Invoice, LineItem
from apps.billing.money import MICRO_CENTS_PER_USD


@pytest.fixture
def ops_client(staff_user):
    client = APIClient()
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def issued_invoice(db, customer_a):
    inv = Invoice.objects.create(
        customer=customer_a, period_start=datetime(2026, 4, 1, tzinfo=dt_tz.utc),
        period_end=datetime(2026, 5, 1, tzinfo=dt_tz.utc), status=Invoice.Status.ISSUED,
        total_micro_cents=90 * MICRO_CENTS_PER_USD, currency="USD", issued_at=timezone.now())
    LineItem.objects.create(invoice=inv, kind="usage", description="Tier 2",
                            units=90000, unit_price_micro_cents=100_000,
                            amount_micro_cents=90 * MICRO_CENTS_PER_USD, tier_ordinal=2)
    return inv


# --- Customer list / detail --------------------------------------------------

@pytest.mark.django_db
def test_customer_list(ops_client, customer_a, customer_b):
    resp = ops_client.get("/ops/customers")
    assert resp.status_code == 200
    assert resp.data["total"] == 2


@pytest.mark.django_db
def test_customer_detail_has_anomaly_signal(ops_client, customer_a):
    resp = ops_client.get(f"/ops/customers/{customer_a.id}")
    assert resp.status_code == 200
    assert "current_period" in resp.data
    assert "anomaly" in resp.data["current_period"]


# --- Credit issuance + idempotency -------------------------------------------

@pytest.mark.django_db
def test_issue_credit_creates_credit_and_audit(ops_client, customer_a):
    resp = ops_client.post(
        f"/ops/customers/{customer_a.id}/credits",
        {"amount_micro_cents": 5 * MICRO_CENTS_PER_USD, "reason": "service outage refund"},
        format="json", HTTP_IDEMPOTENCY_KEY="key-1")
    assert resp.status_code == 201
    assert Credit.objects.for_customer(customer_a).count() == 1
    assert AuditLog.objects.filter(action="credit.issue").count() == 1


@pytest.mark.django_db
def test_issue_credit_requires_idempotency_key(ops_client, customer_a):
    resp = ops_client.post(
        f"/ops/customers/{customer_a.id}/credits",
        {"amount_micro_cents": 100, "reason": "missing idem key test"},
        format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_issue_credit_double_click_is_idempotent(ops_client, customer_a):
    body = {"amount_micro_cents": 5 * MICRO_CENTS_PER_USD, "reason": "double click test"}
    r1 = ops_client.post(f"/ops/customers/{customer_a.id}/credits", body,
                         format="json", HTTP_IDEMPOTENCY_KEY="key-dup")
    r2 = ops_client.post(f"/ops/customers/{customer_a.id}/credits", body,
                         format="json", HTTP_IDEMPOTENCY_KEY="key-dup")
    assert r1.status_code == 201
    assert r2.status_code in (200, 201)
    assert r1.data["id"] == r2.data["id"]
    # Exactly one credit and one audit row
    assert Credit.objects.for_customer(customer_a).count() == 1
    assert AuditLog.objects.filter(action="credit.issue").count() == 1


@pytest.mark.django_db
def test_reused_key_different_body_conflicts(ops_client, customer_a):
    ops_client.post(f"/ops/customers/{customer_a.id}/credits",
                    {"amount_micro_cents": 100, "reason": "first body here ok"},
                    format="json", HTTP_IDEMPOTENCY_KEY="key-x")
    resp = ops_client.post(f"/ops/customers/{customer_a.id}/credits",
                           {"amount_micro_cents": 999, "reason": "different body here"},
                           format="json", HTTP_IDEMPOTENCY_KEY="key-x")
    assert resp.status_code == 409
    assert Credit.objects.for_customer(customer_a).count() == 1


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_credit_same_key_one_credit(customer_a, staff_user):
    N = 6
    barrier = threading.Barrier(N)

    def worker():
        try:
            barrier.wait()
            c = APIClient()
            c.force_authenticate(user=staff_user)
            c.post(f"/ops/customers/{customer_a.id}/credits",
                   {"amount_micro_cents": 5 * MICRO_CENTS_PER_USD, "reason": "concurrent test x"},
                   format="json", HTTP_IDEMPOTENCY_KEY="race-key")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert Credit.objects.for_customer(customer_a).count() == 1
    assert AuditLog.objects.filter(action="credit.issue").count() == 1


# --- Line-item override ------------------------------------------------------

@pytest.mark.django_db
def test_override_line_item_recomputes_total_and_audits(ops_client, issued_invoice):
    line = issued_invoice.line_items.first()
    resp = ops_client.patch(
        f"/ops/invoices/{issued_invoice.id}/line-items/{line.id}",
        {"amount_micro_cents": 50 * MICRO_CENTS_PER_USD, "reason": "rate correction applied"},
        format="json")
    assert resp.status_code == 200
    issued_invoice.refresh_from_db()
    assert issued_invoice.total_micro_cents == 50 * MICRO_CENTS_PER_USD
    audit = AuditLog.objects.filter(action="line_item.override").first()
    assert audit is not None
    assert audit.before["amount_micro_cents"] == 90 * MICRO_CENTS_PER_USD
    assert audit.after["amount_micro_cents"] == 50 * MICRO_CENTS_PER_USD


@pytest.mark.django_db
def test_override_blocked_on_paid_invoice(ops_client, issued_invoice):
    issued_invoice.status = Invoice.Status.PAID
    issued_invoice.save()
    line = issued_invoice.line_items.first()
    resp = ops_client.patch(
        f"/ops/invoices/{issued_invoice.id}/line-items/{line.id}",
        {"amount_micro_cents": 1, "reason": "should be blocked here"},
        format="json")
    assert resp.status_code == 422


# --- Auth isolation ----------------------------------------------------------

@pytest.mark.django_db
def test_customer_api_key_cannot_reach_ops(api_key_a):
    client = APIClient()
    resp = client.get("/ops/customers",
                      HTTP_AUTHORIZATION=f"Bearer {api_key_a.plaintext_key}")
    # No staff session → DRF SessionAuthentication yields no user → 403
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_unauthenticated_cannot_reach_ops(db):
    client = APIClient()
    resp = client.get("/ops/customers")
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_non_staff_user_cannot_reach_ops(db):
    from django.contrib.auth.models import User
    non_staff = User.objects.create_user(username="joe", password="x", is_staff=False)
    client = APIClient()
    client.force_authenticate(user=non_staff)
    resp = client.get("/ops/customers")
    assert resp.status_code == 403
