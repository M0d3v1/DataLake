"""Audit logging for raw-payload-migration operator actions.

`apps.auditing.AuditLog` is tenant-scoped (see apps/auditing/models.py),
but the migration tool's views run in the *public* schema (a platform
operator picks an organization from a cross-tenant list -- see
apps.opsui.views). This helper enters the target organization's schema
just long enough to write the audit row, so each org's own audit trail
records operator actions taken on its data, same as any other
tenant-scoped action.
"""

from typing import Any

from django.db import models
from django_tenants.utils import schema_context

from apps.auditing.service import record_audit_event
from apps.orgs.models import Organization


def record_migration_audit_event(
    organization: Organization,
    *,
    actor,
    action: str,
    target: models.Model,
    metadata: dict[str, Any] | None = None,
) -> None:
    with schema_context(organization.schema_name):
        record_audit_event(actor=actor, action=action, target=target, metadata=metadata or {})
