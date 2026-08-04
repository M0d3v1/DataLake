import uuid

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class AuditLog(TimeStampedModel):
    """An immutable record of a significant platform action -- connection
    created, credential rotated, pipeline run triggered, etc. Tenant-scoped:
    each organization only ever sees its own audit trail."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Null for system-initiated actions (e.g. scheduled runs).",
    )
    action = models.CharField(max_length=255, help_text='e.g. "connection.created"')
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AuditLog({self.action}, {self.target_type}:{self.target_id})"
