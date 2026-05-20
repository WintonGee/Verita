"""
Payment webhook: signature verification + replay safety + idempotency.

Correctness boundaries:
  - invalid signature → 401, no state change
  - stale timestamp → 400
  - same delivery_id three times → one paid transition, two no-ops
  - amount mismatch → 422 + audit, no state change
  - already-paid invoice → idempotent no-op
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone as dt_tz

import pytest
from django.test import Client
from django.utils import timezone

from apps.audit.models import AuditLog, WebhookDelivery
from apps.billing.models import Invoice
from apps.billing.money import MICRO_CENTS_PER_USD

WEBHOOK_SECRET = "dev-webhook-secret-current"  # matches settings_test inheritance
URL = "/webhooks/payments"


@pytest.fixture
def issued_invoice(db, customer_a):
    return Invoice.objects.create(
        customer=customer_a,
        period_start=datetime(2026, 4, 1, tzinfo=dt_tz.utc),
        period_end=datetime(2026, 5, 1, tzinfo=dt_tz.utc),
        status=Invoice.Status.ISSUED,
        currency="USD",
        total_micro_cents=90 * MICRO_CENTS_PER_USD,
        issued_at=timezone.now(),
    )


def _sign(body_bytes, ts=None, secret=WEBHOOK_SECRET):
    ts = ts or str(int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body_bytes, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _post(client, invoice, amount=None, delivery_id=None, ts=None, secret=WEBHOOK_SECRET,
          sig_override=None):
    payload = {
        "invoice_id": str(invoice.id),
        "amount_paid_micro_cents": amount if amount is not None else invoice.total_micro_cents,
        "currency": "USD",
    }
    body = json.dumps(payload).encode()
    sig = sig_override or _sign(body, ts=ts, secret=secret)
    return client.post(
        URL, data=body, content_type="application/json",
        HTTP_X_SIGNATURE=sig,
        HTTP_X_DELIVERY_ID=delivery_id or f"dlv-{uuid.uuid4()}",
    )


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_valid_webhook_marks_invoice_paid(client, issued_invoice):
    resp = _post(client, issued_invoice)
    assert resp.status_code == 200
    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.PAID
    assert issued_invoice.paid_at is not None
    # Audit row written
    assert AuditLog.objects.filter(action="invoice.pay",
                                   resource_id=str(issued_invoice.id)).exists()


@pytest.mark.django_db
def test_invalid_signature_rejected(client, issued_invoice):
    resp = _post(client, issued_invoice, sig_override="t=%d,v1=deadbeef" % int(time.time()))
    assert resp.status_code == 401
    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.ISSUED  # unchanged
    assert WebhookDelivery.objects.filter(
        result=WebhookDelivery.Result.REJECTED_SIGNATURE).exists()


@pytest.mark.django_db
def test_stale_timestamp_rejected(client, issued_invoice):
    old_ts = str(int(time.time()) - 3600)  # 1 hour ago
    resp = _post(client, issued_invoice, ts=old_ts)
    assert resp.status_code == 400
    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.ISSUED
    assert WebhookDelivery.objects.filter(
        result=WebhookDelivery.Result.REJECTED_STALE).exists()


@pytest.mark.django_db
def test_replay_same_delivery_id_three_times(client, issued_invoice):
    delivery_id = f"dlv-{uuid.uuid4()}"
    r1 = _post(client, issued_invoice, delivery_id=delivery_id)
    r2 = _post(client, issued_invoice, delivery_id=delivery_id)
    r3 = _post(client, issued_invoice, delivery_id=delivery_id)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert r2.json()["status"] == "duplicate"
    assert r3.json()["status"] == "duplicate"

    # Exactly one paid transition, one delivery row, one audit row
    assert WebhookDelivery.objects.filter(delivery_id=delivery_id).count() == 1
    assert AuditLog.objects.filter(action="invoice.pay",
                                   resource_id=str(issued_invoice.id)).count() == 1


@pytest.mark.django_db
def test_different_delivery_id_on_paid_invoice_is_noop(client, issued_invoice):
    _post(client, issued_invoice, delivery_id="dlv-1")
    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.PAID
    first_paid_at = issued_invoice.paid_at

    # A second, differently-keyed delivery for the same (now paid) invoice
    resp = _post(client, issued_invoice, delivery_id="dlv-2")
    assert resp.status_code == 200
    issued_invoice.refresh_from_db()
    # Still paid, paid_at unchanged, only one pay-audit row
    assert issued_invoice.paid_at == first_paid_at
    assert AuditLog.objects.filter(action="invoice.pay",
                                   resource_id=str(issued_invoice.id)).count() == 1


@pytest.mark.django_db
def test_amount_mismatch_rejected(client, issued_invoice):
    resp = _post(client, issued_invoice, amount=1 * MICRO_CENTS_PER_USD)  # wrong
    assert resp.status_code == 422
    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.ISSUED  # unchanged
    assert AuditLog.objects.filter(
        action="invoice.payment_rejected_mismatch",
        resource_id=str(issued_invoice.id)).exists()


@pytest.mark.django_db
def test_unknown_invoice_returns_404(client, customer_a):
    fake = type("X", (), {"id": uuid.uuid4(), "total_micro_cents": 0})()
    resp = _post(client, fake)
    assert resp.status_code == 404


@pytest.mark.django_db
def test_previous_secret_accepted_during_rotation(client, issued_invoice, settings):
    settings.WEBHOOK_SECRET_CURRENT = "new-secret"
    settings.WEBHOOK_SECRET_PREVIOUS = WEBHOOK_SECRET
    # Sign with the PREVIOUS secret — should still be accepted
    resp = _post(client, issued_invoice, secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    issued_invoice.refresh_from_db()
    assert issued_invoice.status == Invoice.Status.PAID
