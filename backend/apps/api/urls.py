from django.urls import path

from apps.api import views_v1

urlpatterns = [
    path("v1/events", views_v1.EventIngestView.as_view(), name="v1-events"),
]
