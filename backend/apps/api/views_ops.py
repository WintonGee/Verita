"""
Ops console API. Staff-only (Django auth.User with is_staff). Every mutating
action writes an audit row in the same transaction as its effect.

Auth model: Django session (SessionAuthentication) + IsAdminUser. Distinct
from the customer surface — a customer session/API key cannot reach /ops, and
a staff session cannot reach /v1 (different authentication classes entirely).
"""

import hashlib
import json
from datetime import timedelta

from django.contrib.auth import authenticate, login, logout
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import TruncDay
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import generics, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.pagination import StandardPageNumberPagination
from apps.api.serializers_ops import (
    IssueCreditSerializer,
    OpsApiKeySerializer,
    OpsCustomerListSerializer,
    OpsInvoiceSerializer,
    OverrideLineItemSerializer,
)
from apps.audit.models import IdempotencyKey
from apps.audit.services import write_audit
from apps.billing.models import Credit, Invoice, LineItem, UsageWindow
from apps.tenancy.models import ApiKey, Customer

ANOMALY_MULTIPLIER = 10
IDEMPOTENCY_TTL = timedelta(hours=24)


# --- Ops auth ----------------------------------------------------------------

class OpsLoginView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        user = authenticate(request._request, username=username, password=password)
        if user is None or not user.is_staff:
            return Response(
                {"error": {"code": "unauthenticated", "message": "Invalid staff credentials."}},
                status=status.HTTP_401_UNAUTHORIZED)
        login(request._request, user)
        return Response({"user": {"username": user.username, "email": user.email}})


class OpsLogoutView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]

    def post(self, request):
        logout(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class OpsMeView(APIView):
    # Sets the csrftoken cookie so the SPA can send X-CSRFToken on mutations
    # (DRF SessionAuthentication enforces CSRF on unsafe methods).
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]

    def get(self, request):
        u = request.user
        return Response({"user": {"username": u.username, "email": u.email}})


# --- Customers ---------------------------------------------------------------

class OpsCustomerListView(generics.ListAPIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]
    serializer_class = OpsCustomerListSerializer
    pagination_class = StandardPageNumberPagination

    def get_queryset(self):
        qs = Customer.objects.select_related("price_plan").order_by("name")
        q = self.request.query_params.get("q")
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(name__icontains=q) | Q(billing_email__icontains=q))
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs


def _anomaly_signal(customer):
    """
    Compare today's units to the customer's 30-day daily average. Flag if
    today exceeds ANOMALY_MULTIPLIER × the average. Computed on read — cheap at
    5k customers; at 10× we'd precompute a baseline table (DESIGN.md §4).
    """
    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    daily = (UsageWindow.objects.for_customer(customer)
             .filter(window_start__gte=thirty_days_ago, window_start__lt=today_start)
             .annotate(day=TruncDay("window_start"))
             .values("day")
             .annotate(units=Sum("units_consumed")))
    daily_totals = [d["units"] for d in daily]
    avg_daily = (sum(daily_totals) / len(daily_totals)) if daily_totals else 0

    today_units = (UsageWindow.objects.for_customer(customer)
                   .filter(window_start__gte=today_start)
                   .aggregate(s=Sum("units_consumed"))["s"] or 0)

    is_anomaly = bool(avg_daily and today_units > ANOMALY_MULTIPLIER * avg_daily)
    return {
        "today_units": int(today_units),
        "thirty_day_daily_avg": round(avg_daily, 1),
        "anomaly": is_anomaly,
        "multiplier_threshold": ANOMALY_MULTIPLIER,
    }


class OpsCustomerDetailView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]

    def get(self, request, id):
        customer = (Customer.objects.select_related("price_plan")
                    .filter(id=id).first())
        if customer is None:
            return Response(
                {"error": {"code": "not_found", "message": "Customer not found."}},
                status=status.HTTP_404_NOT_FOUND)

        invoices = (Invoice.objects.for_customer(customer)
                    .prefetch_related("line_items")
                    .order_by("-period_start")[:12])
        api_keys = ApiKey.objects.filter(customer=customer).order_by("-created_at")

        return Response({
            "id": str(customer.id),
            "name": customer.name,
            "billing_email": customer.billing_email,
            "status": customer.status,
            "price_plan": {"id": str(customer.price_plan_id),
                           "name": customer.price_plan.name},
            "current_period": _anomaly_signal(customer),
            "invoices": OpsInvoiceSerializer(invoices, many=True).data,
            "api_keys": OpsApiKeySerializer(api_keys, many=True).data,
        })


# --- Money-moving actions ----------------------------------------------------

class IssueCreditView(APIView):
    """
    POST /ops/customers/{id}/credits  — Idempotency-Key header required.

    Idempotency at two layers:
      - IdempotencyKey table (staff-scoped): replay returns the stored response;
        a reused key with a different body → 409 conflict.
      - Credit UNIQUE(customer, idempotency_key): DB backstop that serializes
        concurrent double-clicks; the loser returns the existing credit.
    Audit row is written in the same transaction as the credit.
    """
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        idem_key = request.headers.get("Idempotency-Key")
        if not idem_key:
            return Response(
                {"error": {"code": "invalid_request",
                           "message": "Idempotency-Key header is required."}},
                status=status.HTTP_400_BAD_REQUEST)

        customer = Customer.objects.filter(id=id).first()
        if customer is None:
            return Response(
                {"error": {"code": "not_found", "message": "Customer not found."}},
                status=status.HTTP_404_NOT_FOUND)

        serializer = IssueCreditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data["amount_micro_cents"]
        reason = serializer.validated_data["reason"]
        staff_id = request.user.get_username()
        body_hash = hashlib.sha256(
            json.dumps(serializer.validated_data, sort_keys=True, default=str).encode()
        ).digest()

        # Replay check
        existing_key = IdempotencyKey.objects.filter(
            staff_id=staff_id, key=idem_key).first()
        if existing_key:
            if bytes(existing_key.request_hash) != body_hash:
                return Response(
                    {"error": {"code": "idempotency_conflict",
                               "message": "Idempotency-Key reused with a different payload."}},
                    status=status.HTTP_409_CONFLICT)
            return Response(existing_key.response_body, status=existing_key.response_status)

        try:
            with transaction.atomic():
                credit = Credit.objects.create(
                    customer=customer,
                    amount_micro_cents=amount,
                    reason=reason,
                    issued_by_staff_id=staff_id,
                    idempotency_key=idem_key,
                )
                write_audit(
                    actor_type="staff", actor_id=staff_id,
                    action="credit.issue",
                    resource_type="credit", resource_id=credit.id,
                    after={"amount_micro_cents": amount, "customer_id": str(customer.id)},
                    reason=reason,
                    request_ip=request.META.get("REMOTE_ADDR"),
                )
                response_body = {
                    "id": str(credit.id),
                    "amount_micro_cents": credit.amount_micro_cents,
                    "reason": credit.reason,
                    "applied_to_invoice_id": None,
                    "created_at": credit.created_at.isoformat(),
                }
                IdempotencyKey.objects.create(
                    customer=customer, staff_id=staff_id, key=idem_key,
                    method="POST", path=request.path, request_hash=body_hash,
                    response_status=status.HTTP_201_CREATED,
                    response_body=response_body,
                    expires_at=timezone.now() + IDEMPOTENCY_TTL,
                )
        except IntegrityError:
            # Concurrent double-click: Credit UNIQUE(customer, key) caught it.
            existing = (Credit.objects.for_customer(customer)
                        .filter(idempotency_key=idem_key).first())
            return Response({
                "id": str(existing.id),
                "amount_micro_cents": existing.amount_micro_cents,
                "reason": existing.reason,
                "applied_to_invoice_id": (str(existing.applied_to_invoice_id)
                                          if existing.applied_to_invoice_id else None),
                "created_at": existing.created_at.isoformat(),
            }, status=status.HTTP_200_OK)

        return Response(response_body, status=status.HTTP_201_CREATED)


class OverrideLineItemView(APIView):
    """
    PATCH /ops/invoices/{invoice_id}/line-items/{line_item_id}

    Locks the line item, captures before/after, updates, recomputes the invoice
    total, and writes an audit row — all in one transaction. Disallowed on paid
    invoices (a paid invoice is corrected with a credit, not a silent edit).
    """
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAdminUser]

    def patch(self, request, invoice_id, line_item_id):
        serializer = OverrideLineItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_amount = serializer.validated_data["amount_micro_cents"]
        new_description = serializer.validated_data.get("description")
        reason = serializer.validated_data["reason"]
        staff_id = request.user.get_username()

        with transaction.atomic():
            line_item = (LineItem.objects.select_for_update()
                         .filter(id=line_item_id, invoice_id=invoice_id)
                         .select_related("invoice").first())
            if line_item is None:
                return Response(
                    {"error": {"code": "not_found", "message": "Line item not found."}},
                    status=status.HTTP_404_NOT_FOUND)

            invoice = line_item.invoice
            if invoice.status == Invoice.Status.PAID:
                return Response(
                    {"error": {"code": "validation_failed",
                               "message": "Cannot override a paid invoice; issue a credit instead."}},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY)

            before = {
                "amount_micro_cents": line_item.amount_micro_cents,
                "description": line_item.description,
            }
            line_item.amount_micro_cents = new_amount
            if new_description is not None:
                line_item.description = new_description
            line_item.overridden_at = timezone.now()
            line_item.override_reason = reason
            line_item.save(update_fields=[
                "amount_micro_cents", "description", "overridden_at", "override_reason"])

            # Recompute the denormalized invoice total.
            new_total = (LineItem.objects.filter(invoice=invoice)
                         .aggregate(s=Sum("amount_micro_cents"))["s"] or 0)
            invoice.total_micro_cents = max(0, new_total)
            invoice.save(update_fields=["total_micro_cents"])

            write_audit(
                actor_type="staff", actor_id=staff_id,
                action="line_item.override",
                resource_type="line_item", resource_id=line_item.id,
                before=before,
                after={"amount_micro_cents": new_amount,
                       "description": line_item.description,
                       "invoice_total_micro_cents": invoice.total_micro_cents},
                reason=reason,
                request_ip=request.META.get("REMOTE_ADDR"),
            )

        return Response({
            "id": str(line_item.id),
            "amount_micro_cents": line_item.amount_micro_cents,
            "description": line_item.description,
            "overridden_at": line_item.overridden_at.isoformat(),
            "override_reason": line_item.override_reason,
            "invoice_total_micro_cents": invoice.total_micro_cents,
        })
