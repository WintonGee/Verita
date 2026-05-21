"""Serializers for the ops console API."""

from rest_framework import serializers

from apps.billing.models import Invoice
from apps.tenancy.models import ApiKey, Customer


class OpsCustomerListSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="price_plan.name", read_only=True)

    class Meta:
        model = Customer
        fields = ["id", "name", "billing_email", "status", "plan_name", "created_at"]


class OpsApiKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiKey
        fields = ["id", "prefix", "name", "created_at", "last_used_at", "revoked_at"]


class OpsInvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ["id", "period_start", "period_end", "status",
                  "total_micro_cents", "currency", "issued_at", "paid_at"]


class IssueCreditSerializer(serializers.Serializer):
    amount_micro_cents = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(min_length=10, max_length=2000)


class OverrideLineItemSerializer(serializers.Serializer):
    amount_micro_cents = serializers.IntegerField(min_value=0)
    description = serializers.CharField(max_length=500, required=False)
    reason = serializers.CharField(min_length=10, max_length=2000)
