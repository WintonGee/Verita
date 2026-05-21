"""
Audit log, webhook deliveries, idempotency keys.

The audit_log table is immutable at the DB layer (trigger + revoked grants;
see the migration). UPDATE/DELETE attempts by `app_role` raise
"permission denied", so this is true at the SQL level, not just at the
application level.
"""

import uuid

from django.db import models


# --- Audit log ---------------------------------------------------------------

class AuditLog(models.Model):
    class ActorType(models.TextChoices):
        STAFF = "staff"
        SYSTEM = "system"
        CUSTOMER = "customer"

    # bigserial; not exposed
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor_id = models.CharField(max_length=255)
    action = models.CharField(max_length=64)
    resource_type = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=64)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField()
    reason = models.TextField(blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(
                fields=["resource_type", "resource_id", "-created_at"],
                name="audit_resource_idx",
            ),
            models.Index(fields=["-created_at"], name="audit_created_idx"),
            models.Index(fields=["actor_type", "actor_id"], name="audit_actor_idx"),
        ]

    def __str__(self):
        return f"audit[{self.action}] {self.resource_type}/{self.resource_id} by {self.actor_id}"


# --- Webhook deliveries (for replay dedup) -----------------------------------

class WebhookDelivery(models.Model):
    class Result(models.TextChoices):
        ACCEPTED = "accepted"
        REJECTED_SIGNATURE = "rejected_signature"
        REJECTED_STALE = "rejected_stale"
        DUPLICATE = "duplicate"
        ERROR = "error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery_id = models.CharField(max_length=128, unique=True)
    received_at = models.DateTimeField(auto_now_add=True)
    signature_valid = models.BooleanField()
    payload_sha256 = models.BinaryField()
    payload = models.JSONField()
    processed_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=24, choices=Result.choices)
    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "webhook_delivery"
        indexes = [
            models.Index(fields=["-received_at"], name="webhook_received_idx"),
        ]


# --- Idempotency keys --------------------------------------------------------

class IdempotencyKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "tenancy.Customer", on_delete=models.RESTRICT, null=True, blank=True,
        related_name="idempotency_keys",
    )
    staff_id = models.CharField(max_length=255, null=True, blank=True)
    key = models.CharField(max_length=255)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=500)
    request_hash = models.BinaryField()
    response_status = models.SmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "idempotency_key"
        constraints = [
            # Partial uniques: customer-scoped OR staff-scoped, not both
            models.UniqueConstraint(
                fields=["customer", "key"],
                condition=models.Q(customer__isnull=False),
                name="idemp_customer_key_unique",
            ),
            models.UniqueConstraint(
                fields=["staff_id", "key"],
                condition=models.Q(staff_id__isnull=False),
                name="idemp_staff_key_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="idemp_expires_idx"),
        ]
