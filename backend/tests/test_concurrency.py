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
from django.db import connections
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
