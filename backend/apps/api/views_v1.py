"""
Customer-facing /v1 endpoints.

POST /v1/events is the workhorse: batched, idempotent, hot-path. The
idempotency guarantee is the schema's UNIQUE(request_id), not application
locking. The hot path is a single multi-row INSERT with ON CONFLICT DO NOTHING
+ a subquery that computes `is_late` (sealed-window check) per event.
"""

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.auth import ApiKeyAuthentication
from apps.api.permissions import HasCustomerScope
from apps.api.serializers import (
    EventIngestBatchSerializer,
    EventIngestResponseSerializer,
)


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
                COALESCE(
                    (SELECT sealed_at IS NOT NULL FROM usage_window
                       WHERE customer_id = %s::uuid
                         AND window_start = date_trunc('hour', r.ts)),
                    FALSE
                ),
                NULL
            FROM UNNEST(
                %s::text[], %s::text[], %s::integer[], %s::timestamptz[]
            ) AS r(request_id, endpoint, units, ts)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING request_id
        """
        params = [
            customer_id, api_key_id, customer_id,
            request_ids, endpoints, units, timestamps,
        ]

        with connection.cursor() as cur:
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
