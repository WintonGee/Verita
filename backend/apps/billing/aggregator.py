"""
Hourly aggregator: events → usage_windows.

Idempotent by construction:
  - A global advisory lock (pg_try_advisory_lock) ensures only one aggregator
    runs at a time; a second invocation returns immediately.
  - The UPSERT recomputes the FULL window total (SUM over all events in the
    hour), so re-running produces the same result.
  - Sealed windows are immune: the ON CONFLICT clause has
    `WHERE usage_window.sealed_at IS NULL`.

Watermark: we track `ingested_at` of the last processed event range in
cron_state. Each run processes windows touched by events ingested since the
previous run (minus a small overlap, to absorb events that landed during the
prior run). Late events (old event_timestamp, recent ingested_at) are picked
up because the candidate scan is on ingested_at but the recompute sums by
event_timestamp.
"""

import logging
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger("verita.aggregator")

# Stable 64-bit-ish key for the global aggregator advisory lock.
AGGREGATOR_LOCK_KEY = "verita:aggregator"

# Don't aggregate the bleeding edge — gives in-flight events of the current
# minute a chance to land. Not a seal; just churn reduction.
EDGE_DELAY = timedelta(minutes=5)
# Overlap re-scans a window of ingested_at to catch events that committed
# during the previous run.
WATERMARK_OVERLAP = timedelta(minutes=5)


def _try_advisory_lock(cur, key: str) -> bool:
    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [key])
    return cur.fetchone()[0]


def _advisory_unlock(cur, key: str) -> None:
    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", [key])


def run_aggregation(now=None, catch_up=False) -> dict:
    """
    Returns a summary dict: {"locked": bool, "windows_upserted": int,
    "candidates": int}. Safe to call repeatedly.

    catch_up=True ignores the edge delay (cutoff = now) so events ingested in
    the last few minutes are still processed. Useful operationally for a forced
    re-aggregation, and for the seed -> aggregate -> invoice demo flow.
    """
    now = now or timezone.now()
    cutoff = now if catch_up else now - EDGE_DELAY

    with connection.cursor() as cur:
        if not _try_advisory_lock(cur, AGGREGATOR_LOCK_KEY):
            logger.info("aggregator already running; skipping")
            return {"locked": False, "windows_upserted": 0, "candidates": 0}

        try:
            return _aggregate(cur, cutoff, catch_up=catch_up)
        finally:
            _advisory_unlock(cur, AGGREGATOR_LOCK_KEY)


def _aggregate(cur, cutoff, catch_up=False) -> dict:
    from apps.billing.models import CronState

    state, _ = CronState.objects.get_or_create(name="aggregator")
    watermark = state.last_run_at
    # catch_up ignores the watermark entirely: scan all events up to cutoff.
    scan_from = None if catch_up else (
        (watermark - WATERMARK_OVERLAP) if watermark else None
    )

    # 1. Find (customer_id, window_start) pairs touched since the watermark.
    if scan_from is not None:
        cur.execute(
            """
            SELECT DISTINCT customer_id, date_trunc('hour', event_timestamp) AS window_start
              FROM event
             WHERE ingested_at >= %s AND ingested_at < %s
            """,
            [scan_from, cutoff],
        )
    else:
        cur.execute(
            """
            SELECT DISTINCT customer_id, date_trunc('hour', event_timestamp) AS window_start
              FROM event
             WHERE ingested_at < %s
            """,
            [cutoff],
        )
    candidates = cur.fetchall()  # list of (customer_id, window_start)
    if not candidates:
        _set_watermark(cutoff)
        return {"locked": True, "windows_upserted": 0, "candidates": 0}

    # 2. Group candidate windows by customer so we can take a per-customer
    #    advisory lock (same lock the invoicer uses) around each chunk.
    by_customer: dict = {}
    for customer_id, window_start in candidates:
        by_customer.setdefault(str(customer_id), []).append(window_start)

    upserted = 0
    for customer_id, windows in by_customer.items():
        with transaction.atomic():
            # Per-customer lock: serializes against the invoicer for this
            # customer (xact-scoped, auto-released on commit).
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [f"verita:billing:{customer_id}"],
            )
            cur.execute(
                """
                INSERT INTO usage_window (
                    id, customer_id, window_start,
                    units_consumed, event_count, last_recomputed_at
                )
                SELECT
                    gen_random_uuid(), customer_id,
                    date_trunc('hour', event_timestamp),
                    SUM(units_consumed), COUNT(*), NOW()
                  FROM event
                 WHERE customer_id = %s::uuid
                   AND date_trunc('hour', event_timestamp) = ANY(%s::timestamptz[])
                 GROUP BY customer_id, date_trunc('hour', event_timestamp)
                ON CONFLICT (customer_id, window_start)
                DO UPDATE SET
                    units_consumed = EXCLUDED.units_consumed,
                    event_count = EXCLUDED.event_count,
                    last_recomputed_at = NOW()
                  WHERE usage_window.sealed_at IS NULL
                """,
                [customer_id, windows],
            )
            upserted += cur.rowcount

    _set_watermark(cutoff)
    logger.info("aggregator: %d windows upserted from %d candidates",
                upserted, len(candidates))
    return {"locked": True, "windows_upserted": upserted, "candidates": len(candidates)}


def _set_watermark(cutoff) -> None:
    from apps.billing.models import CronState

    CronState.objects.update_or_create(
        name="aggregator", defaults={"last_run_at": cutoff}
    )
