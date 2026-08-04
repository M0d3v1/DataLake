import json
import uuid
from typing import Any

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.secrets.base import get_secret_store


class Connection(TimeStampedModel):
    """A configured source or destination: which connector to use and its
    non-secret configuration. Credential material lives separately, in
    `Credential` (see below), never inline here."""

    class Kind(models.TextChoices):
        SOURCE = "source", "Source"
        DESTINATION = "destination", "Destination"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Registry key from apps.connectors (e.g. "rest_api", "sqlserver").
    connector_type = models.CharField(max_length=100)
    config = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["name", "kind"], name="unique_connection_name_per_kind")
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind}/{self.connector_type})"


class Credential(TimeStampedModel):
    """Points at the encrypted credential payload for one Connection.

    The full credential dict (whatever the connection's AuthProvider
    needs -- api_key, token, username/password, ...) is stored as a
    single JSON blob through `apps.secrets.SecretStore`; this row only
    holds the opaque `secret_ref` plus which AuthProvider to use.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.OneToOneField(
        Connection, on_delete=models.CASCADE, related_name="credential"
    )
    # Registry key from apps.authproviders (e.g. "bearer", "api_key").
    auth_provider_type = models.CharField(max_length=100)
    secret_ref = models.CharField(max_length=255)

    @classmethod
    def create_for_connection(
        cls, connection: Connection, auth_provider_type: str, payload: dict[str, Any]
    ) -> "Credential":
        secret_ref = get_secret_store().store(json.dumps(payload))
        return cls.objects.create(
            connection=connection, auth_provider_type=auth_provider_type, secret_ref=secret_ref
        )

    def resolve(self) -> dict[str, Any]:
        """Decrypt and return the credential payload. Callers should hold
        this only transiently (for the duration of a connection test or
        pipeline run step), never persist or log it."""
        raw = get_secret_store().retrieve(self.secret_ref)
        return json.loads(raw)

    def __str__(self) -> str:
        return f"Credential({self.auth_provider_type}) for {self.connection_id}"


class AllowedOutboundHost(TimeStampedModel):
    """Tenant-level override of the outbound HTTP security policy
    (apps.core.outbound_http): a hostname this organization has
    explicitly approved to be contacted even though it resolves to a
    private/loopback/link-local address -- e.g. an internal enterprise
    system reachable from the worker network. See
    docs/decisions/0006-outbound-http-security-policy.md.

    Deliberately just a hostname allowlist, not a general firewall
    config: adding a row here does not disable any other part of the
    outbound policy (scheme validation, redirect handling, response size
    limits, ...), it only lifts the private/loopback/link-local block for
    that one host.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.CharField(max_length=255, unique=True)
    reason = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    def save(self, *args, **kwargs):
        self.host = self.host.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.host
