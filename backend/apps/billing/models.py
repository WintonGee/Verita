"""
Billing core: pricing plans, usage events, hourly windows, monthly invoices,
line items, and credits. All money is bigint micro-cents (1e-8 USD).

Tenant scoping: every billable model uses CustomerScopedManager as `objects`.
A bare `.all()` raises CustomerScopeMissing; call sites must go through
`.for_customer(c)` or the explicit `.unsafe_all_tenants()`.
"""

import uuid

from django.core.validators import MinValueValidator
from django.db import models

from .managers import CustomerScopedManager


# --- Pricing -----------------------------------------------------------------

class PricePlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "price_plan"

    def __str__(self):
        return f"{self.name} [{self.currency}]"


class PriceTier(models.Model):
    """
    Tier covers [start_unit, end_unit). end_unit NULL means infinity.
    Tiers in a plan must cover [0, ∞) without gaps — validated at plan creation
    time, not by a DB constraint (would require an EXCLUDE constraint and is
    overkill for the take-home scope).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    price_plan = models.ForeignKey(
        PricePlan, on_delete=models.CASCADE, related_name="tiers"
    )
    start_unit = models.BigIntegerField(validators=[MinValueValidator(0)])
    end_unit = models.BigIntegerField(null=True, blank=True)  # exclusive; NULL = ∞
    unit_price_micro_cents = models.BigIntegerField(validators=[MinValueValidator(0)])
    ordinality = models.SmallIntegerField()

    class Meta:
        db_table = "price_tier"
        constraints = [
            models.UniqueConstraint(
                fields=["price_plan", "start_unit"], name="price_tier_unique_start",
            ),
            models.CheckConstraint(
                check=models.Q(end_unit__isnull=True) | models.Q(end_unit__gt=models.F("start_unit")),
                name="price_tier_end_gt_start",
            ),
            models.CheckConstraint(
                check=models.Q(unit_price_micro_cents__gte=0),
                name="price_tier_nonneg_price",
            ),
        ]
        ordering = ["price_plan", "ordinality"]

    def __str__(self):
        return f"[{self.start_unit}, {self.end_unit or '∞'}) @ {self.unit_price_micro_cents}μ¢"


# --- Events ------------------------------------------------------------------

class Event(models.Model):
    """
    Append-only usage event. `request_id` is the idempotency key for ingest.
    `is_late` is set TRUE at insert time if the event's hour-window is already
    sealed (via a subquery in the ingest path); also set TRUE by the invoicer's
    Step 10 race-closure sweep for events that landed during invoicer txn.
    """
    # bigserial; never exposed to clients
    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(
        "tenancy.Customer", on_delete=models.RESTRICT, related_name="events"
    )
    api_key = models.ForeignKey(
        "tenancy.ApiKey", on_delete=models.RESTRICT, related_name="events"
    )
    request_id = models.CharField(max_length=64)
    endpoint = models.CharField(max_length=255)
    units_consumed = models.IntegerField(validators=[MinValueValidator(0)])
    event_timestamp = models.DateTimeField()
    ingested_at = models.DateTimeField(auto_now_add=True)
    is_late = models.BooleanField(default=False)
    adjusted_at = models.DateTimeField(null=True, blank=True)

    objects = CustomerScopedManager()

    class Meta:
        db_table = "event"
        constraints = [
            models.UniqueConstraint(fields=["request_id"], name="event_request_id_unique"),
            models.CheckConstraint(
                check=models.Q(units_consumed__gte=0),
                name="event_units_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["customer", "-event_timestamp"],
                name="event_customer_ts_idx",
            ),
            models.Index(
                fields=["api_key", "-event_timestamp"],
                name="event_apikey_ts_idx",
            ),
            # Late-event sweep (covers the per-customer partial scan)
            models.Index(
                fields=["customer"],
                name="event_late_unadj_idx",
                condition=models.Q(is_late=True, adjusted_at__isnull=True),
            ),
        ]

    def __str__(self):
        return f"event[{self.request_id}] customer={self.customer_id} units={self.units_consumed}"


# --- Usage windows -----------------------------------------------------------

class UsageWindow(models.Model):
    """
    Hourly rollup of events. Recomputable until sealed; sealed at invoice
    issuance for the period that contains this window.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "tenancy.Customer", on_delete=models.RESTRICT, related_name="usage_windows"
    )
    window_start = models.DateTimeField()  # date_trunc('hour', ts)
    units_consumed = models.BigIntegerField(validators=[MinValueValidator(0)])
    event_count = models.IntegerField(validators=[MinValueValidator(0)])
    sealed_at = models.DateTimeField(null=True, blank=True)
    last_recomputed_at = models.DateTimeField(auto_now=True)

    objects = CustomerScopedManager()

    class Meta:
        db_table = "usage_window"
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "window_start"],
                name="window_unique_customer_start",
            ),
        ]
        indexes = [
            models.Index(
                fields=["customer", "-window_start"],
                name="window_customer_start_idx",
            ),
            # Aggregator's "what's still recomputable" scan
            models.Index(
                fields=["window_start"],
                name="window_unsealed_idx",
                condition=models.Q(sealed_at__isnull=True),
            ),
        ]


# --- Invoices ----------------------------------------------------------------

class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        ISSUED = "issued"
        PAID = "paid"
        VOID = "void"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "tenancy.Customer", on_delete=models.RESTRICT, related_name="invoices"
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    total_micro_cents = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default="USD")
    issued_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_delivery_id = models.CharField(max_length=128, null=True, blank=True)

    objects = CustomerScopedManager()

    class Meta:
        db_table = "invoice"
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "period_start"], name="invoice_unique_customer_period",
            ),
            models.CheckConstraint(
                check=models.Q(total_micro_cents__gte=0),
                name="invoice_total_nonneg",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "period_end"], name="invoice_status_period_idx"),
            models.Index(fields=["customer", "-period_start"], name="invoice_customer_period_idx"),
        ]


class LineItem(models.Model):
    class Kind(models.TextChoices):
        USAGE = "usage"
        CREDIT_APPLICATION = "credit_application"
        ADJUSTMENT = "adjustment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.RESTRICT, related_name="line_items"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    description = models.CharField(max_length=500)
    units = models.BigIntegerField(default=0)
    unit_price_micro_cents = models.BigIntegerField(default=0)
    amount_micro_cents = models.BigIntegerField()  # can be negative for credit_application
    tier_ordinal = models.SmallIntegerField(null=True, blank=True)
    overridden_at = models.DateTimeField(null=True, blank=True)
    override_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "line_item"
        indexes = [
            models.Index(fields=["invoice"], name="lineitem_invoice_idx"),
        ]


# --- Credits -----------------------------------------------------------------

class Credit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "tenancy.Customer", on_delete=models.RESTRICT, related_name="credits"
    )
    amount_micro_cents = models.BigIntegerField(validators=[MinValueValidator(1)])
    reason = models.TextField()
    issued_by_staff_id = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=255)
    applied_to_invoice = models.ForeignKey(
        Invoice, on_delete=models.RESTRICT, null=True, blank=True,
        related_name="applied_credits",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CustomerScopedManager()

    class Meta:
        db_table = "credit"
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "idempotency_key"],
                name="credit_unique_idemp",
            ),
            models.CheckConstraint(
                check=models.Q(amount_micro_cents__gt=0),
                name="credit_amount_positive",
            ),
        ]
        indexes = [
            # Invoice generator finds pending credits
            models.Index(
                fields=["customer"],
                name="credit_pending_idx",
                condition=models.Q(applied_to_invoice__isnull=True),
            ),
        ]
