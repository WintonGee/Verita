"""
Request/response serializers. Keep these to validation only — view logic
lives in views_v1.py.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers


MAX_EVENTS_PER_BATCH = 1000
FUTURE_TIMESTAMP_TOLERANCE = timedelta(minutes=5)


class EventIngestItemSerializer(serializers.Serializer):
    request_id = serializers.CharField(max_length=64, min_length=1)
    endpoint = serializers.CharField(max_length=255)
    units_consumed = serializers.IntegerField(min_value=0)
    timestamp = serializers.DateTimeField()

    def validate_timestamp(self, value):
        max_future = timezone.now() + FUTURE_TIMESTAMP_TOLERANCE
        if value > max_future:
            raise serializers.ValidationError(
                f"timestamp is more than {FUTURE_TIMESTAMP_TOLERANCE} in the future"
            )
        return value


class EventIngestBatchSerializer(serializers.Serializer):
    events = EventIngestItemSerializer(many=True, allow_empty=False)

    def validate_events(self, value):
        if len(value) > MAX_EVENTS_PER_BATCH:
            raise serializers.ValidationError(
                f"batch size {len(value)} exceeds limit {MAX_EVENTS_PER_BATCH}"
            )
        return value


class EventIngestResultItemSerializer(serializers.Serializer):
    request_id = serializers.CharField()
    status = serializers.ChoiceField(choices=["accepted", "duplicate"])


class EventIngestResponseSerializer(serializers.Serializer):
    results = EventIngestResultItemSerializer(many=True)
