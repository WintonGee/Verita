"""
Single canonical error shape across the API.

  {"error": {"code": "...", "message": "...", "details": {...}}}

DRF's default handler returns various shapes; we normalize here. Hooked up via
REST_FRAMEWORK["EXCEPTION_HANDLER"].
"""

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
    if isinstance(exc, ValidationError):
        return "validation_failed"
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        return "unauthenticated"
    if isinstance(exc, PermissionDenied):
        return "forbidden"
    if isinstance(exc, NotFound):
        return "not_found"
    if isinstance(exc, Throttled):
        return "rate_limited"
    return "internal_error"


def error_response_handler(exc, context):
    drf_response = drf_default_handler(exc, context)
    if drf_response is None:
        return None
    detail = drf_response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail.pop("detail"))
        details = detail or None
    else:
        message = str(detail)
        details = None
    body = {"error": {"code": _code_for(exc), "message": message}}
    if details:
        body["error"]["details"] = details
    return Response(body, status=drf_response.status_code, headers=drf_response.headers)
