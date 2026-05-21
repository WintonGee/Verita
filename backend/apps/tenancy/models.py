"""
Tenancy: the customer (tenant), the API keys they use server-side, and the
customer users who log in to the dashboard.

API keys are high-entropy random secrets — we store SHA-256(salt || secret)
plus a non-secret prefix for fast lookup. The plaintext key is shown once on
creation and never recoverable.

Customer users live in their own table, distinct from Django's auth.User
(which is reserved for ops staff). This makes it structurally impossible for
a staff middleware to authenticate a customer user, or vice versa.
"""

import secrets
import uuid

from django.db import models


class Customer(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        SUSPENDED = "suspended"
        CLOSED = "closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    billing_email = models.EmailField(max_length=255)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    price_plan = models.ForeignKey(
        "billing.PricePlan",
        on_delete=models.RESTRICT,
        related_name="customers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer"
        indexes = [
            models.Index(fields=["status"], name="customer_status_idx",
                         condition=models.Q(status="active")),
        ]

    def __str__(self):
        return f"{self.name} ({self.id})"


class CustomerUser(models.Model):
    """
    A login identity for the customer dashboard. Separate from auth.User
    (which is for ops staff). Custom authentication backend reads the
    customer_session cookie and resolves to this row.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer, on_delete=models.RESTRICT, related_name="users"
    )
    email = models.EmailField(max_length=255, unique=True)
    password_hash = models.CharField(max_length=255)  # set via django.contrib.auth.hashers.make_password
    is_active = models.BooleanField(default=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customer_user"

    def __str__(self):
        return f"{self.email} → {self.customer.name}"


class CustomerSession(models.Model):
    """
    Browser session for the customer dashboard. The cookie carries a random
    token; we store only its SHA-256 hash (a DB leak can't be replayed as a
    live session). Distinct from Django's auth.User sessions (ops staff), so
    the two auth domains never share a cookie namespace.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer_user = models.ForeignKey(
        CustomerUser, on_delete=models.CASCADE, related_name="sessions"
    )
    token_hash = models.BinaryField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "customer_session"
        indexes = [
            models.Index(fields=["expires_at"], name="session_expires_idx"),
        ]


def _new_api_key_prefix():
    # 8 random base62-ish chars (we use base16 here for simplicity in dev).
    # Collision space ≈ 16^8 = 4 billion; we retry on the unique-constraint
    # violation if it ever happens.
    return secrets.token_hex(4)


class ApiKey(models.Model):
    """
    Server-to-server credentials. The plaintext key is shown ONCE at creation:
        vk_live_<8-hex-prefix>_<32-char-random-secret>
    We store:
        - prefix: the 8 hex chars, for hot-path lookup (indexed, unique)
        - key_hash: SHA-256(salt || secret), 32 bytes
        - salt:    per-key random, 16 bytes
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customer, on_delete=models.RESTRICT, related_name="api_keys"
    )
    prefix = models.CharField(max_length=12, unique=True, default=_new_api_key_prefix)
    key_hash = models.BinaryField()
    salt = models.BinaryField()
    name = models.CharField(max_length=255, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "api_key"
        indexes = [
            models.Index(fields=["customer", "revoked_at"], name="apikey_customer_revoked_idx"),
        ]

    def __str__(self):
        active = "revoked" if self.revoked_at else "active"
        return f"vk_live_{self.prefix}…  [{active}, customer={self.customer_id}]"
