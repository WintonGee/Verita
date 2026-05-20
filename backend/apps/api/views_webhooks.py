"""
Payment webhook: POST /webhooks/payments

Stripe-style HMAC verification, replay-safe, idempotent.

  Header  X-Signature: t=<unix_ts>,v1=<hex_hmac_sha256>
  Header  X-Delivery-ID: <unique delivery id>
  Body    {"invoice_id": "...", "amount_paid_micro_cents": N, "currency": "USD"}

Signed payload is `f"{t}.{raw_body}"`. We accept the current secret and an
optional previous secret (rotation window). Timestamp must be within ±5 min.
Replays are deduped by UNIQUE(delivery_id). The only state transition is
invoice issued → paid; the amount is verified but never used to mutate the
contract total.

Implemented as a plain csrf-exempt Django view so we can read the raw body
for HMAC and bypass DRF content negotiation.
"""

import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.audit.services import write_audit
from apps.audit.models import WebhookDelivery
from apps.billing.models import Invoice

logger = logging.getLogger("verita.webhook")


def _error(code, message, status):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _parse_signature(header: str) -> dict:
    parts = {}
    for piece in (header or "").split(","):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def _valid_signature(timestamp: str, raw_body: bytes, provided_hex: str) -> bool:
    signed_payload = timestamp.encode() + b"." + raw_body
    secrets_to_try = [settings.WEBHOOK_SECRET_CURRENT]
    if settings.WEBHOOK_SECRET_PREVIOUS:
        secrets_to_try.append(settings.WEBHOOK_SECRET_PREVIOUS)
    for secret in secrets_to_try:
        if not secret:
            continue
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided_hex or ""):
            return True
    return False


@csrf_exempt
@require_POST
def payments_webhook(request):
    raw_body = request.body
    sig = _parse_signature(request.META.get("HTTP_X_SIGNATURE", ""))
    delivery_id = request.META.get("HTTP_X_DELIVERY_ID", "")
    ts = sig.get("t", "")
    provided = sig.get("v1", "")

    if not delivery_id:
        return _error("invalid_request", "Missing X-Delivery-ID", 400)
    if not ts or not provided:
        return _error("invalid_request", "Missing or malformed X-Signature", 400)

    # 1. Timestamp window (protects against indefinite replay of stolen sigs)
    try:
        ts_int = int(ts)
    except ValueError:
        return _error("invalid_request", "Bad timestamp", 400)
    if abs(time.time() - ts_int) > settings.WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS:
        _record_delivery(delivery_id, False, raw_body, WebhookDelivery.Result.REJECTED_STALE)
        return _error("invalid_request", "Timestamp outside tolerance window", 400)

    # 2. Signature
    if not _valid_signature(ts, raw_body, provided):
        _record_delivery(delivery_id, False, raw_body, WebhookDelivery.Result.REJECTED_SIGNATURE)
        return _error("unauthenticated", "Invalid signature", 401)

    # 3. Replay dedup + process atomically
    try:
        with transaction.atomic():
            delivery = WebhookDelivery.objects.create(
                delivery_id=delivery_id,
                signature_valid=True,
                payload_sha256=hashlib.sha256(raw_body).digest(),
                payload=json.loads(raw_body or b"{}"),
                result=WebhookDelivery.Result.ACCEPTED,
            )
            return _process_payment(delivery, json.loads(raw_body or b"{}"),
                                    request.META.get("REMOTE_ADDR"))
    except IntegrityError:
        # Duplicate delivery_id — already processed. Idempotent success.
        logger.info("webhook replay ignored: delivery_id=%s", delivery_id)
        return JsonResponse({"status": "duplicate"}, status=200)


def _process_payment(delivery, payload, ip):
    invoice_id = payload.get("invoice_id")
    amount_paid = payload.get("amount_paid_micro_cents")

    invoice = (Invoice.objects.unsafe_all_tenants()
               .select_for_update().filter(id=invoice_id).first())
    if invoice is None:
        delivery.result = WebhookDelivery.Result.ERROR
        delivery.error_message = f"invoice {invoice_id} not found"
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=["result", "error_message", "processed_at"])
        return _error("not_found", "Invoice not found", 404)

    # Amount must match the contract total. We never *use* the webhook amount
    # to mutate the total — a mismatch is suspicious and rejected.
    if amount_paid != invoice.total_micro_cents:
        delivery.result = WebhookDelivery.Result.ERROR
        delivery.error_message = (
            f"amount mismatch paid={amount_paid} expected={invoice.total_micro_cents}")
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=["result", "error_message", "processed_at"])
        write_audit(
            actor_type="system", actor_id="payment_webhook",
            action="invoice.payment_rejected_mismatch",
            resource_type="invoice", resource_id=invoice.id,
            after={"paid": amount_paid, "expected": invoice.total_micro_cents,
                   "delivery_id": delivery.delivery_id},
            request_ip=ip,
        )
        return _error("validation_failed", "Amount does not match invoice total", 422)

    if invoice.status == Invoice.Status.ISSUED:
        invoice.status = Invoice.Status.PAID
        invoice.paid_at = timezone.now()
        invoice.payment_delivery_id = delivery.delivery_id
        invoice.save(update_fields=["status", "paid_at", "payment_delivery_id"])
        write_audit(
            actor_type="system", actor_id="payment_webhook",
            action="invoice.pay",
            resource_type="invoice", resource_id=invoice.id,
            before={"status": "issued"},
            after={"status": "paid", "delivery_id": delivery.delivery_id},
            request_ip=ip,
        )
    elif invoice.status == Invoice.Status.PAID:
        pass  # idempotent no-op
    else:
        delivery.result = WebhookDelivery.Result.ERROR
        delivery.error_message = f"invoice status={invoice.status}, cannot mark paid"
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=["result", "error_message", "processed_at"])
        return _error("validation_failed", f"Invoice is {invoice.status}", 422)

    delivery.processed_at = timezone.now()
    delivery.save(update_fields=["processed_at"])
    return JsonResponse({"status": "ok"}, status=200)


def _record_delivery(delivery_id, sig_valid, raw_body, result):
    """Best-effort record of a rejected delivery (outside the main txn)."""
    try:
        WebhookDelivery.objects.create(
            delivery_id=delivery_id,
            signature_valid=sig_valid,
            payload_sha256=hashlib.sha256(raw_body).digest(),
            payload={},
            result=result,
        )
    except IntegrityError:
        pass  # already recorded
