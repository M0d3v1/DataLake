"""Shared test scaffolding for apps.opsui UI tests.

`TenantTestCase` (django_tenants.test.cases) creates `self.tenant` plus a
real `Domain` row at `tenant.test.com` and adds it to `ALLOWED_HOSTS` --
requests with that Host header route through `TenantMainMiddleware` to
`config.urls` (the tenant urlconf) with `request.tenant` set to
`self.tenant`. Requests with a Host header that matches no Domain (e.g.
the Django test client's default `testserver`) fall through to
`config.urls_public` instead, since `SHOW_PUBLIC_IF_NO_TENANT_FOUND=True`
-- see docs/decisions/0008-internal-operator-ui.md. `PUBLIC_HOST` here is
made explicit (and added to ALLOWED_HOSTS) rather than relying on
Django's implicit `testserver` handling.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse
from django_tenants.test.cases import TenantTestCase

from apps.orgs.models import Membership, Organization

User = get_user_model()

PUBLIC_HOST = "ops.test.internal"


def public_reverse(viewname, **kwargs):
    """`reverse()` against `config.urls_public` explicitly -- the
    thread-local "current" urlconf `reverse()` otherwise defaults to is
    `ROOT_URLCONF` (the tenant urlconf), which doesn't have the `opsui`
    (public) namespace at all until a request has actually gone through
    `TenantMainMiddleware` and called `set_urlconf()`."""
    return reverse(viewname, kwargs=kwargs, urlconf="config.urls_public")


def tenant_reverse(viewname, **kwargs):
    return reverse(viewname, kwargs=kwargs, urlconf="config.urls")


class OpsUITestCase(TenantTestCase):
    def setUp(self):
        super().setUp()
        # NOTE: deliberately NOT using the `@override_settings(...)` class
        # decorator here -- `TenantTestCase.setUpClass` (django_tenants)
        # never calls `super().setUpClass()`, so Django's own
        # `SimpleTestCase.setUpClass` (which is what actually applies a
        # class-decorator's `_overridden_settings`) never runs, and the
        # decorator silently no-ops. The *context-manager* form
        # (`self.settings(...)`, via `enable()`/`disable()` directly) has
        # no such dependency, so use that instead, entered once per test
        # via `enterContext` for automatic cleanup.
        self.enterContext(
            self.settings(ALLOWED_HOSTS=["testserver", PUBLIC_HOST, "tenant.test.com"])
        )
        # A request to the public host (any public_get/public_post call)
        # makes TenantMainMiddleware call connection.set_schema_to_public()
        # for the duration of that request -- and that's a change to
        # global connection state that outlives the request, since
        # TenantTestCase only resets the schema once per class
        # (setUpClass), not per test method. Reset explicitly before
        # every test so one test's public-host request can never leave
        # the next test unable to see this class's own tenant schema.
        connection.set_tenant(self.tenant)

    def tenant_get(self, path, **kwargs):
        return self.client.get(path, HTTP_HOST=self.domain.domain, **kwargs)

    def tenant_post(self, path, data=None, **kwargs):
        return self.client.post(path, data or {}, HTTP_HOST=self.domain.domain, **kwargs)

    def public_get(self, path, **kwargs):
        try:
            return self.client.get(path, HTTP_HOST=PUBLIC_HOST, **kwargs)
        finally:
            # TenantMainMiddleware leaves the connection on the public
            # schema after a public-host request (correctly, for that
            # request) -- restore this class's own tenant schema
            # afterward so the rest of the test can keep making ordinary
            # tenant-scoped ORM calls without remembering to do this
            # itself.
            connection.set_tenant(self.tenant)

    def public_post(self, path, data=None, **kwargs):
        try:
            return self.client.post(path, data or {}, HTTP_HOST=PUBLIC_HOST, **kwargs)
        finally:
            connection.set_tenant(self.tenant)

    def make_user(self, username, *, is_staff=False, password="pw") -> User:
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.test",
            password=password,
            is_staff=is_staff,
        )

    def make_member(self, username, role, *, organization=None) -> User:
        user = self.make_user(username)
        Membership.objects.create(user=user, organization=organization or self.tenant, role=role)
        return user

    def login(self, user, password="pw"):
        self.client.login(username=user.username, password=password)

    def create_other_org(self, schema_name: str, name: str, slug: str) -> Organization:
        """Create a second Organization for a cross-tenant-denial test.
        `Organization.save()` (django-tenants' `TenantMixin`) refuses to
        create a new tenant unless the *public* schema is currently
        active, and `auto_create_schema=True` means creating it also
        switches/creates its own schema -- restore this class's own
        tenant schema afterward either way."""
        connection.set_schema_to_public()
        try:
            return Organization.objects.create(schema_name=schema_name, name=name, slug=slug)
        finally:
            connection.set_tenant(self.tenant)

    def delete_other_org(self, organization: Organization) -> None:
        connection.set_schema_to_public()
        try:
            organization.delete(force_drop=True)
        finally:
            connection.set_tenant(self.tenant)
