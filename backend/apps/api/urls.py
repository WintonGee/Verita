from django.urls import path

from apps.api import views_v1, views_webhooks

urlpatterns = [
    path("v1/events", views_v1.EventIngestView.as_view(), name="v1-events"),
    path("webhooks/payments", views_webhooks.payments_webhook, name="webhooks-payments"),
]
