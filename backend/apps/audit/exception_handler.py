"""
Single canonical error shape across the API.

  {"error": {"code": "...", "message": "...", "details": {...}}}

DRF's default handler returns various shapes; we normalize here. Hooked up via
REST_FRAMEWORK["EXCEPTION_HANDLER"].
"""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_handler


def _code_for(exc):
    # DRF's default handler maps Django's Http404 / PermissionDenied to the
    # right *status*, but hands us the original exception — so we must match
    # both the DRF and the Django types or a 404 mislabels as "internal_error".
    if isinstance(exc, ValidationError):
        return "validation_failed"
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        return "unauthenticated"
    if isinstance(exc, (PermissionDenied, DjangoPermissionDenied)):
        return "forbidden"
    if isinstance(exc, (NotFound, Http404)):
        return "not_found"
    if isinstance(exc, Throttled):
        return "rate_limited"
    return "internal_error"


def _clean(value):
    """Recursively turn DRF ErrorDetail / nested structures into plain str."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return str(value)


def _first_message(value):
    """Pull a single human-readable line out of a (cleaned) error structure,
    prefixing the field name for per-field errors so the message is useful."""
    if isinstance(value, dict):
        for k, v in value.items():
            m = _first_message(v)
            if m:
                return m if k == "non_field_errors" else f"{k}: {m}"
    elif isinstance(value, (list, tuple)):
        for v in value:
            m = _first_message(v)
            if m:
                return m
    elif isinstance(value, str):
        return value
    return None


def error_response_handler(exc, context):
    drf_response = drf_default_handler(exc, context)
    if drf_response is None:
        return None
    detail = drf_response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
        details = _clean({k: v for k, v in detail.items() if k != "detail"}) or None
    elif isinstance(exc, ValidationError):
        # Field-level validation: surface a clean human message AND structured
        # per-field details, never the raw `{... ErrorDetail(...) ...}` repr.
        cleaned = _clean(detail)
        message = _first_message(cleaned) or "Validation failed."
        details = cleaned if isinstance(cleaned, dict) else None
    else:
        message = str(detail)
        details = None
    body = {"error": {"code": _code_for(exc), "message": message}}
    if details:
        body["error"]["details"] = details
    return Response(body, status=drf_response.status_code, headers=drf_response.headers)
