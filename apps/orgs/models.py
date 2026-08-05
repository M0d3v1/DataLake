import uuid

from django.conf import settings
from django.db import models
from django_tenants.models import DomainMixin, TenantMixin

from apps.core.models import TimeStampedModel


class Organization(TenantMixin, TimeStampedModel):
    """A tenant: one customer organization, mapped 1:1 to a Postgres schema.

    All tenant-scoped data (connections, credentials, pipelines, runs,
    audit log, ...) lives in that schema, physically isolated from every
    other organization. See docs/decisions/0002-schema-per-tenant.md.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    # A stable, non-guessable tenant identifier for contexts where
    # `schema_name` (a human-chosen string) or the auto-incrementing `id`
    # (small, sequential, easy to enumerate) is the wrong thing to use --
    # e.g. raw payload object-storage key prefixes, where the key should
    # not double as a predictable enumeration of every tenant on the
    # deployment. See apps.rawstore.backends.s3.S3RawPayloadStore and
    # docs/decisions/0007-raw-payload-immutability.md.
    tenant_uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    # django-tenants: automatically create/migrate the Postgres schema
    # when an Organization is saved. Schema deletion is NOT automatic
    # (auto_drop_schema stays False) so an accidental Organization.delete()
    # can't silently destroy a tenant's data -- schema teardown is a
    # deliberate, separate operational action.
    auto_create_schema = True
    auto_drop_schema = False

    def __str__(self) -> str:
        return self.name


class Domain(DomainMixin):
    """Hostname routing table used by django-tenants' TenantMainMiddleware
    to resolve an incoming request to an Organization/schema."""


class Membership(TimeStampedModel):
    """A user's role within one organization. Lives in the public schema
    alongside User and Organization since it links across both."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        ENGINEER = "engineer", "Engineer"
        ANALYST = "analyst", "Analyst"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ENGINEER)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="unique_membership_per_org"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization} ({self.role})"
