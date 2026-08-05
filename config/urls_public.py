"""URLconf served for requests whose hostname matches no tenant Domain --
the public schema. This is where the internal operator UI lives (see
apps.opsui and docs/decisions/0008-internal-operator-ui.md): platform
operators and org admins reach it at a plain operator-facing hostname,
not a tenant subdomain, since it needs to work before -- or across --
any single tenant context.
"""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("ops/", include("apps.opsui.urls_public")),
]
