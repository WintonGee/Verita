"""
DRF permission classes for the customer-facing /v1 surface.
"""

from rest_framework import permissions


class HasCustomerScope(permissions.BasePermission):
    """
    Allows the request only if a Customer has been resolved by an upstream
    authentication class. This lives in addition to the manager-level scoping
    so a misconfigured view fails fast with 401, not with a CustomerScopeMissing
    500 error from inside the queryset.
    """

    def has_permission(self, request, view):  # noqa: ARG002
        return getattr(request, "customer", None) is not None
