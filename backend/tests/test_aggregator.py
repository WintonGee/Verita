"""
Aggregator correctness: events → usage_windows.

Claims under test:
  - aggregates events into the correct hourly window
  - idempotent: re-running produces the same window total (the result is
    stable, even though the 5-min watermark overlap may re-scan boundary rows)
  - incremental: new events get folded in on the next run
  - sealed windows are immune to recompute
  - late events (old event_timestamp, recent ingested_at) are aggregated

The aggregator's EDGE_DELAY skips events ingested in the last 5 minutes (churn
reduction). Tests pass a `now` 10 minutes ahead to simulate elapsed time, so
freshly-created events fall inside the aggregation window.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.billing.aggregator import run_aggregation
from apps.billing.models import Event, UsageWindow


def _run_agg():
    return run_aggregation(now=timezone.now() + timedelta(minutes=10))


def _make_event(customer, api_key, request_id, units, event_ts):
    return Event.objects.unsafe_all_tenants().create(
        customer=customer, api_key=api_key, request_id=request_id,
        endpoint="/v1/test", units_consumed=units, event_timestamp=event_ts,
    )


def _window(customer, window_start):
    return (UsageWindow.objects.for_customer(customer)
            .filter(window_start=window_start).first())


@pytest.mark.django_db(transaction=True)
def test_aggregates_events_into_hourly_window(customer_a, api_key_a):
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, "r1", 10, hour + timedelta(minutes=5))
    _make_event(customer_a, api_key_a, "r2", 20, hour + timedelta(minutes=30))

    result = _run_agg()
    assert result["locked"] is True

    w = _window(customer_a, hour)
    assert w is not None
    assert w.units_consumed == 30
    assert w.event_count == 2


@pytest.mark.django_db(transaction=True)
def test_rerun_produces_same_total(customer_a, api_key_a):
    """Idempotency: the window total is identical after a second run."""
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, "r1", 10, hour + timedelta(minutes=5))

    _run_agg()
    assert _window(customer_a, hour).units_consumed == 10

    _run_agg()
    w = _window(customer_a, hour)
    assert w.units_consumed == 10  # unchanged — no double counting
    assert w.event_count == 1


@pytest.mark.django_db(transaction=True)
def test_new_events_folded_in_on_next_run(customer_a, api_key_a):
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, "r1", 10, hour + timedelta(minutes=5))
    _run_agg()
    assert _window(customer_a, hour).units_consumed == 10

    _make_event(customer_a, api_key_a, "r2", 25, hour + timedelta(minutes=10))
    _run_agg()
    assert _window(customer_a, hour).units_consumed == 35


@pytest.mark.django_db(transaction=True)
def test_sealed_window_is_not_recomputed(customer_a, api_key_a):
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, "r1", 10, hour + timedelta(minutes=5))
    _run_agg()

    # Seal it (simulating invoice issuance)
    w = _window(customer_a, hour)
    w.sealed_at = timezone.now()
    w.save()

    # A late event for the sealed hour
    _make_event(customer_a, api_key_a, "r2", 999, hour + timedelta(minutes=20))
    _run_agg()

    w = _window(customer_a, hour)
    # Sealed window must NOT have absorbed the late event
    assert w.units_consumed == 10


@pytest.mark.django_db(transaction=True)
def test_catch_up_processes_fresh_events(customer_a, api_key_a):
    """
    A normal run with the default `now` excludes events ingested in the last
    5 minutes (edge delay). catch_up=True ignores the edge delay and the
    watermark, so freshly-ingested events are processed. This is the path the
    seed -> aggregate demo flow relies on.
    """
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _make_event(customer_a, api_key_a, "fresh-1", 10, hour + timedelta(minutes=5))

    # Default run: cutoff = now - 5min; the just-ingested event is excluded.
    run_aggregation()
    assert _window(customer_a, hour) is None

    # catch_up: cutoff = now, watermark ignored → event is processed.
    result = run_aggregation(catch_up=True)
    assert result["locked"] is True
    assert _window(customer_a, hour).units_consumed == 10


@pytest.mark.django_db(transaction=True)
def test_late_event_old_timestamp_recent_ingest_is_aggregated(customer_a, api_key_a):
    """
    An event with an old event_timestamp but recent ingested_at lands in the
    correct (old) hour window and is aggregated, as long as that window isn't
    sealed.
    """
    old_hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=10)
    _make_event(customer_a, api_key_a, "late-1", 42, old_hour + timedelta(minutes=15))

    _run_agg()
    w = _window(customer_a, old_hour)
    assert w is not None
    assert w.units_consumed == 42
