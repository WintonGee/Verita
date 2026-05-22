"""
Concurrency proofs. These tests require real transactions (not the wrapped-
in-rollback default) and threading. Marked `concurrency` so they can be
filtered separately on CI.

The core claim: idempotency lives in the schema's UNIQUE(request_id), not in
the application. Multiple concurrent ingests of the same request_id serialize
at the index level; exactly one row exists in the end.
"""

import threading

import pytest
from django.db import connection, connections, transaction
from django.test import Client
from django.utils import timezone

from apps.billing.models import Event


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_same_request_id_yields_one_row(api_key_a):
    """
    20 threads simultaneously POST the same request_id. After all of them
    finish, exactly 1 Event row should exist.

    The dedup primitive is the schema (UNIQUE(request_id)) — there's no
    application-level lock. We're proving the schema-level guarantee.
    """
    N_THREADS = 20
    barrier = threading.Barrier(N_THREADS)
    results: list[int] = []
    results_lock = threading.Lock()

    payload = {
        "events": [
            {
                "request_id": "race-1",
                "endpoint": "/v1/test",
                "units_consumed": 10,
                "timestamp": timezone.now().isoformat(),
            }
        ]
    }
    auth = f"Bearer {api_key_a.plaintext_key}"

    def worker():
        try:
            barrier.wait()  # release all threads at once
            client = Client()  # not the APIClient; we want raw threading
            resp = client.post(
                "/v1/events", payload, content_type="application/json",
                HTTP_AUTHORIZATION=auth,
            )
            with results_lock:
                results.append(resp.status_code)
        finally:
            # Each thread gets its own DB connection; clean up.
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All requests returned 207 (idempotent: even duplicates are 207, not 4xx)
    assert all(s == 207 for s in results), results
    # But exactly one Event row exists for that request_id
    rows = Event.objects.unsafe_all_tenants().filter(request_id="race-1")
    assert rows.count() == 1


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_seal_exclusive_lock_excludes_ingest_shared_lock(customer_a):
    """
    The seal-race guarantee rests on lock modes: ingest takes the per-customer
    seal lock SHARED, the invoicer takes it EXCLUSIVE. Prove the two can't
    interleave — while a transaction holds it exclusive (the invoicer mid-seal),
    a shared acquisition (an ingest) cannot succeed; once released, it can.
    """
    key = f"verita:seal:{customer_a.id}"
    held = threading.Event()
    release = threading.Event()

    def holder():
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])
                held.set()
                release.wait(timeout=5)
            # leaving atomic() commits, releasing the xact lock
        finally:
            connection.close()

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=5), "holder never acquired the exclusive lock"

    # Exclusive is held by the other txn → a shared try-lock here must fail.
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock_shared(hashtext(%s))", [key])
            got_while_held = cur.fetchone()[0]

    release.set()
    t.join(timeout=5)

    # Released → the shared lock (ingest) is now obtainable.
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock_shared(hashtext(%s))", [key])
            got_after_release = cur.fetchone()[0]

    assert got_while_held is False   # invoicer's exclusive seal lock excludes ingest
    assert got_after_release is True  # after the seal commits, ingest proceeds
