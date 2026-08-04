from typing import Any

from django.db import models

from apps.auditing.models import AuditLog


def record_audit_event(
    *, actor, action: str, target: models.Model, metadata: dict[str, Any] | None = None
) -> AuditLog:
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=type(target).__name__,
        target_id=str(target.pk),
        metadata=metadata or {},
    )
