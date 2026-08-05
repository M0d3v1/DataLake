"""Tenant-schema URLs: served on each organization's own domain, with
`request.tenant`/the active schema already resolved by django-tenants
before these views run -- no org/schema identifier appears in any of
these paths. See config/urls.py and
docs/decisions/0008-internal-operator-ui.md."""

from django.urls import path

from apps.opsui import views

app_name = "opsui_tenant"

urlpatterns = [
    path("", views.tenant_home, name="tenant_home"),
    path("<uuid:pipeline_id>/runs/", views.run_list, name="run_list"),
    path("<uuid:pipeline_id>/runs/<uuid:run_id>/", views.run_detail, name="run_detail"),
    path(
        "<uuid:pipeline_id>/runs/<uuid:run_id>/continue/", views.run_continue, name="run_continue"
    ),
]
