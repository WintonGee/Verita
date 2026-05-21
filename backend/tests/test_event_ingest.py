"""
Event ingest correctness tests. Focus areas (per the brief's correctness
boundaries):
  - Idempotency: replays produce no double-effects
  - Tenant isolation: a key can only ingest for its own customer
  - Input validation: negative units, future timestamps, malformed body
  - Auth: missing/bad/revoked keys; suspended customer

Concurrency tests (50 threads with same request_id) live in
test_concurrency.py and require TransactionTestCase + threading. The
single-process tests here cover the schema-level dedup; the concurrency
suite proves it under contention.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import Event
from apps.tenancy.models import Customer


URL = "/v1/events"


def _auth_header(api_key):
    return f"Bearer {api_key.plaintext_key}"


def _event(request_id, units=10, endpoint="/v1/test", ts=None):
    return {
        "request_id": request_id,
        "endpoint": endpoint,
        "units_consumed": units,
        "timestamp": (ts or timezone.now()).isoformat(),
    }


@pytest.fixture
def client():
    return APIClient()


# --- Happy path --------------------------------------------------------------

@pytest.mark.django_db
def test_single_event_ingested(client, api_key_a):
    resp = client.post(URL, {"events": [_event("req-1")]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 207, resp.data
    assert resp.data["results"] == [{"request_id": "req-1", "status": "accepted"}]
    assert Event.objects.for_customer(api_key_a.customer).count() == 1


@pytest.mark.django_db
def test_batch_ingested(client, api_key_a):
    body = {"events": [_event(f"req-{i}") for i in range(5)]}
    resp = client.post(URL, body, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 207
    assert all(r["status"] == "accepted" for r in resp.data["results"])
    assert Event.objects.for_customer(api_key_a.customer).count() == 5


# --- Idempotency -------------------------------------------------------------

@pytest.mark.django_db
def test_duplicate_request_id_within_batch_returns_duplicate(client, api_key_a):
    body = {"events": [_event("req-1"), _event("req-1", units=999)]}
    resp = client.post(URL, body, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 207
    statuses = [r["status"] for r in resp.data["results"]]
    # One accepted, one duplicate (order-preserving)
    assert sorted(statuses) == ["accepted", "duplicate"]
    # Only one row inserted; the first wins, second is a no-op
    assert Event.objects.for_customer(api_key_a.customer).count() == 1
    saved = Event.objects.for_customer(api_key_a.customer).first()
    assert saved.units_consumed == 10  # first one wins


@pytest.mark.django_db
def test_replay_batch_is_full_no_op(client, api_key_a):
    body = {"events": [_event("req-1"), _event("req-2"), _event("req-3")]}
    first = client.post(URL, body, format="json",
                        HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert all(r["status"] == "accepted" for r in first.data["results"])

    second = client.post(URL, body, format="json",
                         HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert all(r["status"] == "duplicate" for r in second.data["results"])
    assert Event.objects.for_customer(api_key_a.customer).count() == 3


@pytest.mark.django_db
def test_request_id_globally_unique_across_tenants(client, api_key_a, api_key_b):
    """
    request_id is globally unique per the brief. If two different tenants
    happen to send the same request_id, the second one is rejected (silently
    via DO NOTHING). This is the design — request_ids should be UUID-shaped
    in practice, so collisions are negligible. The test pins the behavior.
    """
    client.post(URL, {"events": [_event("shared-req")]}, format="json",
                HTTP_AUTHORIZATION=_auth_header(api_key_a))
    resp = client.post(URL, {"events": [_event("shared-req")]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_b))
    assert resp.data["results"][0]["status"] == "duplicate"
    # Customer A has the row; customer B does not
    assert Event.objects.for_customer(api_key_a.customer).count() == 1
    assert Event.objects.for_customer(api_key_b.customer).count() == 0


# --- Auth & tenant isolation -------------------------------------------------

@pytest.mark.django_db
def test_no_auth_header_returns_401(client):
    resp = client.post(URL, {"events": [_event("req-1")]}, format="json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_bad_api_key_returns_401(client):
    resp = client.post(URL, {"events": [_event("req-1")]}, format="json",
                       HTTP_AUTHORIZATION="Bearer vk_live_00000000_00000000000000000000000000000000")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_revoked_api_key_returns_401(client, api_key_a):
    api_key_a.revoked_at = timezone.now()
    api_key_a.save()
    resp = client.post(URL, {"events": [_event("req-1")]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 401


@pytest.mark.django_db
def test_suspended_customer_cannot_ingest(client, api_key_a):
    api_key_a.customer.status = Customer.Status.SUSPENDED
    api_key_a.customer.save()
    resp = client.post(URL, {"events": [_event("req-1")]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 401


# --- Validation --------------------------------------------------------------

@pytest.mark.django_db
def test_negative_units_rejected(client, api_key_a):
    resp = client.post(URL, {"events": [_event("req-1", units=-1)]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 400
    assert Event.objects.for_customer(api_key_a.customer).count() == 0


@pytest.mark.django_db
def test_far_future_timestamp_rejected(client, api_key_a):
    far_future = timezone.now() + timedelta(hours=1)
    resp = client.post(URL, {"events": [_event("req-1", ts=far_future)]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 400


@pytest.mark.django_db
def test_past_timestamp_accepted(client, api_key_a):
    past = timezone.now() - timedelta(days=2)
    resp = client.post(URL, {"events": [_event("req-1", ts=past)]}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 207


@pytest.mark.django_db
def test_empty_batch_rejected(client, api_key_a):
    resp = client.post(URL, {"events": []}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 400


@pytest.mark.django_db
def test_oversized_batch_rejected(client, api_key_a):
    events = [_event(f"req-{i}") for i in range(1001)]
    resp = client.post(URL, {"events": events}, format="json",
                       HTTP_AUTHORIZATION=_auth_header(api_key_a))
    assert resp.status_code == 400
