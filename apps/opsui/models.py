import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.orgs.models import Organization

# How long a job may sit in RUNNING with no progress before the UI treats
# it as stalled (worker died mid-run) and offers a retry override. Not a
# heartbeat/lease system -- an honest, documented heuristic, same
# operational trade-off apps.execution already makes for a hard-killed
# worker leaving a PipelineRun stuck RUNNING (see ADR 0005).
STALE_JOB_THRESHOLD = timedelta(minutes=15)


class RawPayloadMigrationJob(TimeStampedModel):
    """One dry-run inspection or real migration pass of an organization's
    raw payload objects from a legacy (unprefixed, or schema-name
    prefixed) object-storage key to the current tenant-UUID-prefixed key
    -- see apps.rawstore.migration and ADR 0007.

    Lives in the public schema (SHARED_APPS), not the tenant schema:
    platform operators need to list/monitor jobs across every
    organization without switching tenant context first. The actual
    inspection/migration work this job tracks still happens inside the
    target organization's own tenant schema (RawPayloadRecord and the
    object store are both tenant-scoped) -- apps.opsui.tasks enters that
    schema for the duration of the run.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIALLY_FAILED = "partially_failed", "Partially failed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL_STATUSES = {Status.SUCCEEDED, Status.PARTIALLY_FAILED, Status.FAILED, Status.CANCELLED}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="raw_payload_migration_jobs"
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Null if the initiating user was later deleted.",
    )
    dry_run = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # --- summary counts (apps.rawstore.migration.MigrationInspectionResult) ---
    records_inspected = models.PositiveIntegerField(default=0)
    already_migrated = models.PositiveIntegerField(default=0)
    legacy_unprefixed = models.PositiveIntegerField(default=0)
    schema_prefixed = models.PositiveIntegerField(default=0)
    missing_objects = models.PositiveIntegerField(default=0)
    checksum_conflicts = models.PositiveIntegerField(default=0)
    ready_for_migration = models.PositiveIntegerField(default=0)
    records_migrated = models.PositiveIntegerField(default=0)
    records_skipped = models.PositiveIntegerField(default=0)

    # Bounded list of safe identifiers only -- record id, run id,
    # sequence, category, a short human-readable reason, and a truncated
    # checksum prefix for debugging. NEVER raw payload bytes, object-store
    # credentials, signed URLs, secrets, or tokens -- see
    # apps.rawstore.migration._safe_problem_entry.
    sample_problem_records = models.JSONField(default=list, blank=True)

    # Sanitized via apps.core.redaction, same discipline as
    # PipelineRun.error_message -- never raw exception text that might
    # echo storage credentials or request/response content.
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(status__in=["pending", "running"]),
                name="one_active_raw_payload_migration_job_per_org",
            )
        ]

    def __str__(self) -> str:
        mode = "dry-run" if self.dry_run else "migration"
        return f"RawPayloadMigrationJob({self.organization_id}, {mode}, {self.status})"

    @property
    def is_stale(self) -> bool:
        return self.status == self.Status.RUNNING and (
            timezone.now() - self.updated_at > STALE_JOB_THRESHOLD
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES
