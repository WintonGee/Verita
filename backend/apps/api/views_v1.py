"""
Customer-facing /v1 endpoints.

POST /v1/events is the workhorse: batched, idempotent, hot-path. The
idempotency guarantee is the schema's UNIQUE(request_id), not application
locking. The hot path is a single multi-row INSERT with ON CONFLICT DO NOTHING
+ a subquery that computes `is_late` (already-invoiced check) per event, under a
shared advisory lock that coordinates with the invoicer's exclusive seal lock.
"""

import base64
import json
from datetime import datetime, timezone as dt_tz

from django.db import connection, transaction
from django.db.models import Count, Sum
from django.db.models.functions import Trunc
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.auth import ApiKeyAuthentication, CustomerSessionAuthentication
from apps.api.pagination import StandardPageNumberPagination
from apps.api.permissions import HasCustomerScope
from apps.api.serializers import (
    EventIngestBatchSerializer,
    EventIngestResponseSerializer,
)
from apps.api.serializers_read import (
    InvoiceDetailSerializer,
    InvoiceListSerializer,
    UsageResponseSerializer,
)
from apps.billing.models import Event, Invoice

# Both API key and session cookie authenticate the customer read endpoints.
CUSTOMER_AUTH = [ApiKeyAuthentication, CustomerSessionAuthentication]


@extend_schema(
    request=EventIngestBatchSerializer,
    responses={207: EventIngestResponseSerializer},
    summary="Batched, idempotent usage event ingestion",
)
class EventIngestView(APIView):
    """
    POST /v1/events
      body: {"events": [{"request_id","endpoint","units_consumed","timestamp"}, ...]}
      response 207: {"results": [{"request_id","status": "accepted"|"duplicate"}, ...]}

    Idempotency: replaying any subset of an earlier batch is a no-op.
    Atomicity is per-event, not per-batch: a malformed event would be rejected
    by the serializer before any DB work, and a duplicate request_id is silently
    skipped via ON CONFLICT DO NOTHING.
    """
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasCustomerScope]

    def post(self, request):
        serializer = EventIngestBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data["events"]

        customer_id = str(request.customer.id)
        api_key_id = str(request.api_key.id)

        # Build arrays for the UNNEST bulk insert. One round-trip.
        request_ids = [it["request_id"] for it in items]
        endpoints = [it["endpoint"] for it in items]
        units = [it["units_consumed"] for it in items]
        timestamps = [it["timestamp"] for it in items]

        sql = """
            INSERT INTO event (
                customer_id, api_key_id, request_id, endpoint,
                units_consumed, event_timestamp, ingested_at, is_late, adjusted_at
            )
            SELECT
                %s::uuid, %s::uuid, r.request_id, r.endpoint,
                r.units, r.ts, NOW(),
                -- An event is "late" iff the period containing it has already
                -- been invoiced. The shared/exclusive seal lock makes this check
                -- authoritative: an event either commits before the invoicer's
                -- exclusive lock (and is billed on that invoice) or after it
                -- (and sees the issued invoice here -> flagged late, adjusted
                -- next period). Never lost, never double-billed. A non-existent
                -- hour-window no longer hides a late event (the old check did).
                EXISTS(
                    SELECT 1 FROM invoice
                     WHERE customer_id = %s::uuid
                       AND period_start <= r.ts AND period_end > r.ts
                       AND status <> 'draft'
                ),
                NULL
            FROM UNNEST(
                %s::text[], %s::text[], %s::integer[], %s::timestamptz[]
            ) AS r(request_id, endpoint, units, ts)
            ON CONFLICT (customer_id, request_id) DO NOTHING
            RETURNING request_id
        """
        params = [
            customer_id, api_key_id, customer_id,
            request_ids, endpoints, units, timestamps,
        ]

        # Shared seal lock: ingests run concurrently with each other (shared
        # locks don't conflict), but the monthly invoicer takes the EXCLUSIVE
        # lock on this key, so ingest and the seal step can't interleave. Held
        # until commit, so the EXISTS check above and the insert are atomic
        # w.r.t. the invoicer.
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                    [f"verita:seal:{customer_id}"],
                )
                cur.execute(sql, params)
                inserted = {row[0] for row in cur.fetchall()}

        # An input batch may contain the same request_id more than once
        # (poor client behavior, but legitimately a retry that got merged).
        # The DB inserts at most one row per unique request_id. We mark the
        # FIRST occurrence of each request_id as "accepted" (if in the
        # inserted set), and every subsequent occurrence as "duplicate".
        results = []
        seen_in_batch: set[str] = set()
        for rid in request_ids:
            if rid in seen_in_batch:
                results.append({"request_id": rid, "status": "duplicate"})
            elif rid in inserted:
                results.append({"request_id": rid, "status": "accepted"})
                seen_in_batch.add(rid)
            else:
                results.append({"request_id": rid, "status": "duplicate"})
                seen_in_batch.add(rid)
        return Response({"results": results}, status=status.HTTP_207_MULTI_STATUS)


def _encode_cursor(bucket_dt) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"b": bucket_dt.isoformat()}).encode()).decode()


def _decode_cursor(cursor: str):
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return parse_datetime(data["b"])
    except Exception:  # noqa: BLE001
        return None


class UsageView(APIView):
    """
    GET /v1/usage — usage aggregated into time buckets.

    Query params: start, end (ISO; default = current month so far),
    granularity (hour|day, default hour), api_key_id (filter), cursor, limit.

    Aggregates the `event` table on the fly (so api_key filtering works — the
    usage_window rollup has no per-key dimension). Keyset cursor on the bucket
    timestamp avoids the deep-OFFSET penalty. At larger scale this would read
    pre-aggregated windows for the no-filter case; documented in DESIGN.md.
    """
    authentication_classes = CUSTOMER_AUTH
    permission_classes = [HasCustomerScope]

    @extend_schema(responses={200: UsageResponseSerializer})
    def get(self, request):
        granularity = request.query_params.get("granularity", "hour")
        if granularity not in ("hour", "day"):
            return Response(
                {"error": {"code": "invalid_request",
                           "message": "granularity must be 'hour' or 'day'"}},
                status=status.HTTP_400_BAD_REQUEST)

        now = datetime.now(dt_tz.utc)
        default_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = parse_datetime(request.query_params.get("start", "")) or default_start
        end = parse_datetime(request.query_params.get("end", "")) or now

        try:
            limit = min(int(request.query_params.get("limit", 100)), 1000)
        except ValueError:
            limit = 100

        qs = (Event.objects.for_customer(request.customer)
              .filter(event_timestamp__gte=start, event_timestamp__lt=end))

        api_key_id = request.query_params.get("api_key_id")
        if api_key_id:
            qs = qs.filter(api_key_id=api_key_id)

        qs = (qs.annotate(bucket=Trunc("event_timestamp", granularity))
              .values("bucket")
              .annotate(units_consumed=Sum("units_consumed"),
                        event_count=Count("id"))
              .order_by("-bucket"))

        cursor = request.query_params.get("cursor")
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded is not None:
                qs = qs.filter(bucket__lt=decoded)

        rows = list(qs[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]

        data = [{
            "window_start": r["bucket"],
            "units_consumed": r["units_consumed"],
            "event_count": r["event_count"],
        } for r in rows]
        next_cursor = _encode_cursor(rows[-1]["bucket"]) if has_more and rows else None

        return Response({"data": data, "next_cursor": next_cursor})


class InvoiceListView(generics.ListAPIView):
    """GET /v1/invoices — page+limit (bounded set; see DESIGN.md §6)."""
    authentication_classes = CUSTOMER_AUTH
    permission_classes = [HasCustomerScope]
    serializer_class = InvoiceListSerializer
    pagination_class = StandardPageNumberPagination

    def get_queryset(self):
        qs = Invoice.objects.for_customer(self.request.customer).order_by("-period_start")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs


class InvoiceDetailView(generics.RetrieveAPIView):
    """
    GET /v1/invoices/{id} — tenant-scoped lookup. A cross-tenant id returns
    404 (not 403): the scoped queryset simply doesn't contain it, so it's
    indistinguishable from a non-existent id.
    """
    authentication_classes = CUSTOMER_AUTH
    permission_classes = [HasCustomerScope]
    serializer_class = InvoiceDetailSerializer
    lookup_field = "id"

    def get_queryset(self):
        return (Invoice.objects.for_customer(self.request.customer)
                .prefetch_related("line_items"))
