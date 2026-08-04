"""URLconf served for requests that matched a real tenant Domain.
django-tenants has already resolved and set the current tenant
(`request.tenant`, `connection.tenant`) before these views run -- no URL
here accepts a tenant/schema identifier from the request, deliberately:
which organization's data a view can touch is bound to the domain the
request arrived on, not a user-suppliable parameter. See apps.opsui.urls_tenant
and docs/decisions/0008-internal-operator-ui.md.
"""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("pipelines/", include("apps.opsui.urls_tenant")),
]
