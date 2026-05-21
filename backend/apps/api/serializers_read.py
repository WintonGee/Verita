"""
Read serializers for customer-facing GET endpoints (usage, invoices).
"""

from rest_framework import serializers

from apps.billing.models import Invoice, LineItem


class UsageBucketSerializer(serializers.Serializer):
    window_start = serializers.DateTimeField()
    units_consumed = serializers.IntegerField()
    event_count = serializers.IntegerField()


class UsageResponseSerializer(serializers.Serializer):
    data = UsageBucketSerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)


class LineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineItem
        fields = [
            "id", "kind", "description", "units",
            "unit_price_micro_cents", "amount_micro_cents",
            "tier_ordinal", "overridden_at", "override_reason",
        ]


class InvoiceListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = [
            "id", "period_start", "period_end", "status",
            "total_micro_cents", "currency", "issued_at", "paid_at",
        ]


class InvoiceDetailSerializer(serializers.ModelSerializer):
    line_items = LineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "period_start", "period_end", "status",
            "total_micro_cents", "currency", "issued_at", "paid_at",
            "line_items",
        ]
