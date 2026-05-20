from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api"
    label = "api"

    def ready(self):
        # Register drf-spectacular auth extensions (import side effect).
        from apps.api import schema  # noqa: F401
