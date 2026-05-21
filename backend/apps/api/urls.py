from django.urls import path

from apps.api import views_auth, views_ops, views_v1, views_webhooks

urlpatterns = [
    # Ops console (staff)
    path("ops/auth/login", views_ops.OpsLoginView.as_view(), name="ops-login"),
    path("ops/auth/logout", views_ops.OpsLogoutView.as_view(), name="ops-logout"),
    path("ops/auth/me", views_ops.OpsMeView.as_view(), name="ops-me"),
    path("ops/customers", views_ops.OpsCustomerListView.as_view(), name="ops-customers"),
    path("ops/customers/<uuid:id>", views_ops.OpsCustomerDetailView.as_view(), name="ops-customer-detail"),
    path("ops/customers/<uuid:id>/credits", views_ops.IssueCreditView.as_view(), name="ops-credits"),
    path("ops/invoices/<uuid:invoice_id>/line-items/<uuid:line_item_id>",
         views_ops.OverrideLineItemView.as_view(), name="ops-override-line-item"),

    # Customer auth (dashboard)
    path("v1/auth/login", views_auth.LoginView.as_view(), name="v1-login"),
    path("v1/auth/logout", views_auth.LogoutView.as_view(), name="v1-logout"),
    path("v1/me", views_auth.MeView.as_view(), name="v1-me"),
    # Customer data
    path("v1/events", views_v1.EventIngestView.as_view(), name="v1-events"),
    path("v1/usage", views_v1.UsageView.as_view(), name="v1-usage"),
    path("v1/invoices", views_v1.InvoiceListView.as_view(), name="v1-invoices"),
    path("v1/invoices/<uuid:id>", views_v1.InvoiceDetailView.as_view(), name="v1-invoice-detail"),
    # Payment webhook
    path("webhooks/payments", views_webhooks.payments_webhook, name="webhooks-payments"),
]
