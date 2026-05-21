"""
drf-spectacular preprocessing hooks.

The published OpenAPI schema documents the customer-facing /v1 API (what
external integrators consume). The /ops console is an internal, staff-only
surface driven by our own SPA, so we exclude it from the public schema rather
than half-document it.
"""


def exclude_internal_paths(endpoints):
    return [
        (path, path_regex, method, callback)
        for (path, path_regex, method, callback) in endpoints
        if not path.startswith("/ops")
    ]
