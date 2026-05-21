"""
drf-spectacular extensions so the generated OpenAPI documents our custom
auth scheme. Without this, the schema generator warns it can't resolve
ApiKeyAuthentication.
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiKeyAuthScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api.auth.ApiKeyAuthentication"
    name = "ApiKeyAuth"

    def get_security_definition(self, auto_schema):  # noqa: ARG002
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "API key as `Authorization: Bearer vk_live_<prefix>_<secret>`",
        }


class CustomerSessionAuthScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api.auth.CustomerSessionAuthentication"
    name = "CustomerSessionAuth"

    def get_security_definition(self, auto_schema):  # noqa: ARG002
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": "customer_session",
            "description": "Customer dashboard session cookie (set by /v1/auth/login).",
        }
