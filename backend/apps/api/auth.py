"""
API key authentication for /v1 endpoints.

Header: `Authorization: Bearer vk_live_<prefix>_<secret>`

Lookup is O(1): the 8-char prefix is uniquely indexed. Verification is a
single SHA-256(salt || secret) constant-time compare. Hot-path cost should
be ≤200µs.

A successful auth sets `request.customer` (the tenant). Downstream views go
through CustomerScopedViewSet, which only knows how to query for that
customer.
"""

import hashlib
import hmac
import re

from django.utils import timezone
from rest_framework import authentication, exceptions

from apps.tenancy.models import ApiKey, Customer


KEY_RE = re.compile(r"^vk_live_([0-9a-f]{8})_([0-9a-f]{32})$")


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication class. Returns (None, api_key) on success because we
    don't have a Django auth.User for customer code — instead we stash the
    Customer on the request later. The `user` slot stays None.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith(self.keyword + " "):
            return None  # no API key present; let other auth classes try

        raw = auth_header[len(self.keyword) + 1 :].strip()
        match = KEY_RE.match(raw)
        if not match:
            raise exceptions.AuthenticationFailed("Malformed API key.")

        prefix, secret = match.group(1), match.group(2)
        try:
            key = ApiKey.objects.select_related("customer").get(prefix=prefix)
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid API key.")

        if key.revoked_at is not None:
            raise exceptions.AuthenticationFailed("API key revoked.")

        # Constant-time hash compare
        h = hashlib.sha256()
        h.update(bytes(key.salt))
        h.update(secret.encode())
        if not hmac.compare_digest(h.digest(), bytes(key.key_hash)):
            raise exceptions.AuthenticationFailed("Invalid API key.")

        if key.customer.status != Customer.Status.ACTIVE:
            raise exceptions.AuthenticationFailed(
                f"Customer is {key.customer.status}."
            )

        # Attach to request for view-layer consumption. We avoid using
        # request.user since that's reserved for Django's auth.User (ops staff).
        request.customer = key.customer
        request.api_key = key

        # Bump last_used_at (best-effort; non-blocking)
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())

        return (None, key)

    def authenticate_header(self, request):  # noqa: ARG002
        return self.keyword
