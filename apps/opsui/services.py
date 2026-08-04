"""Application service for the raw payload migration job lifecycle:
creating, retrying, and cancelling `RawPayloadMigrationJob` rows.

This is the layer both the web UI (`apps.opsui.views`) and the
`migrate_raw_payload_storage` management command's web-facing counterpart
call -- but the actual inspection/migration *logic* lives one layer
below, in `apps.rawstore.migration`, and is never duplicated here. This
module's job is dispatch and job-state bookkeeping, not business logic:
creating a job row, handing it to Celery (or, for tests/inline callers,
running it synchronously), and enforcing "one active job per
organization" -- see docs/decisions/0008-internal-operator-ui.md.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.exceptions import ConfigurationError
from apps.opsui.models import RawPayloadMigrationJob
from apps.orgs.models import Organization


def get_active_job(organization: Organization) -> RawPayloadMigrationJob | None:
    return RawPayloadMigrationJob.objects.filter(
        organization=organization,
        status__in=[RawPayloadMigrationJob.Status.PENDING, RawPayloadMigrationJob.Status.RUNNING],
    ).first()


def latest_job(organization: Organization) -> RawPayloadMigrationJob | None:
    return RawPayloadMigrationJob.objects.filter(organization=organization).first()


def start_migration_job(
    organization: Organization, *, initiated_by, dry_run: bool
) -> tuple[RawPayloadMigrationJob, bool]:
    """Create a new job, or return the existing active one, for
    `organization`. Returns `(job, created)`. Never creates a second
    concurrently-active job for the same organization -- enforced both
    here (a fast pre-check for a friendly response) and by the
    `one_active_raw_payload_migration_job_per_org` DB constraint (the
    real guarantee under a race)."""
    active = get_active_job(organization)
    if active is not None:
        return active, False

    try:
        with transaction.atomic():
            job = RawPayloadMigrationJob.objects.create(
                organization=organization, initiated_by=initiated_by, dry_run=dry_run
            )
    except IntegrityError:
        # Lost the race against a concurrent submission for the same org.
        existing = get_active_job(organization)
        if existing is not None:
            return existing, False
        raise
    return job, True


def retry_migration_job(
    job: RawPayloadMigrationJob, *, initiated_by
) -> tuple[RawPayloadMigrationJob, bool]:
    """Start a fresh job for `job.organization`, same `dry_run` mode.
    Safe by construction: `apps.rawstore.migration.run_raw_payload_migration`
    treats an already-migrated record as a cheap, side-effect-free no-op,
    so re-running the whole pass from scratch after any interruption --
    a clean failure, a cancellation, or a worker that died mid-run --
    never duplicates or corrupts work already done.

    A job stuck in RUNNING past `apps.opsui.models.STALE_JOB_THRESHOLD`
    (the worker most likely died without a clean failure -- the same
    documented, operator-driven-recovery gap ADR 0005 describes for a
    hard-killed PipelineRun) is marked CANCELLED here before starting the
    replacement, so the one-active-job-per-org constraint doesn't block
    it. A job still genuinely RUNNING (not stale) cannot be retried --
    raises ConfigurationError."""
    if job.status == RawPayloadMigrationJob.Status.RUNNING:
        if not job.is_stale:
            raise ConfigurationError(
                f"migration job {job.id} is still running; wait for it to finish or cancel it"
            )
        job.status = RawPayloadMigrationJob.Status.CANCELLED
        job.error_message = (
            "marked cancelled: appeared stalled (no progress for longer than "
            "the configured staleness threshold) and was superseded by a manual retry"
        )
        job.finished_at = job.updated_at
        job.save(update_fields=["status", "error_message", "finished_at", "updated_at"])

    return start_migration_job(job.organization, initiated_by=initiated_by, dry_run=job.dry_run)


def request_cancel(job: RawPayloadMigrationJob) -> RawPayloadMigrationJob:
    if job.status not in (
        RawPayloadMigrationJob.Status.PENDING,
        RawPayloadMigrationJob.Status.RUNNING,
    ):
        raise ConfigurationError(f"migration job {job.id} is not running; cannot cancel it")
    job.cancel_requested = True
    job.save(update_fields=["cancel_requested", "updated_at"])
    return job


def resolve_org_or_404(org_id) -> Organization:
    try:
        return Organization.objects.get(pk=org_id)
    except (Organization.DoesNotExist, ValueError, ValidationError) as exc:
        from django.http import Http404

        raise Http404("organization not found") from exc
