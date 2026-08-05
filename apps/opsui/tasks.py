"""Celery execution boundary for a RawPayloadMigrationJob.

Deliberately thin, same shape as `apps.execution.tasks.run_pipeline_task`:
enter the target organization's tenant schema, call
`apps.rawstore.migration.run_raw_payload_migration` (the one place the
actual inspection/migration logic lives), and translate progress/outcome
into updates on the job row. `RawPayloadMigrationJob` itself lives in the
public schema, but -- as with `apps.rawstore.backends.s3.S3RawPayloadStore
._tenant_uuid` looking up `apps.orgs.models.Organization` -- a SHARED_APPS
model's table is reachable via Postgres's search_path regardless of which
schema `django_tenants.utils.schema_context` has made active, so no
schema-switching back and forth is needed mid-task.
"""

from celery import shared_task
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.core.exceptions import DataLakeError
from apps.core.logging import get_logger
from apps.core.redaction import sanitize_error_message
from apps.opsui.models import RawPayloadMigrationJob
from apps.rawstore.migration import MigrationInspectionResult, run_raw_payload_migration

log = get_logger(__name__)

_RESULT_FIELDS = (
    "records_inspected",
    "already_migrated",
    "legacy_unprefixed",
    "schema_prefixed",
    "missing_objects",
    "checksum_conflicts",
    "ready_for_migration",
    "records_migrated",
    "records_skipped",
)


def _apply_result(job: RawPayloadMigrationJob, result: MigrationInspectionResult) -> None:
    for field_name in _RESULT_FIELDS:
        setattr(job, field_name, getattr(result, field_name))
    job.sample_problem_records = result.problem_records
    job.save(update_fields=[*_RESULT_FIELDS, "sample_problem_records", "updated_at"])


@shared_task(bind=True)
def run_raw_payload_migration_task(self, job_id: str) -> None:
    job = RawPayloadMigrationJob.objects.select_related("organization").get(pk=job_id)
    schema_name = job.organization.schema_name

    job.status = RawPayloadMigrationJob.Status.RUNNING
    job.celery_task_id = self.request.id
    job.started_at = timezone.now()
    job.save(update_fields=["status", "celery_task_id", "started_at", "updated_at"])

    def progress_callback(result: MigrationInspectionResult) -> None:
        _apply_result(job, result)

    def cancel_check() -> bool:
        job.refresh_from_db(fields=["cancel_requested"])
        return job.cancel_requested

    try:
        with schema_context(schema_name):
            result = run_raw_payload_migration(
                dry_run=job.dry_run, progress_callback=progress_callback, cancel_check=cancel_check
            )
    except DataLakeError as exc:
        job.status = RawPayloadMigrationJob.Status.FAILED
        job.error_message = sanitize_error_message(str(exc))
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
        log.error("raw_payload_migration.failed", job_id=str(job.id), category=exc.category)
        return
    except Exception as exc:  # noqa: BLE001 -- convert unclassified errors into a
        # recorded, non-crashing job failure rather than an opaque Celery
        # traceback the operator UI can't render.
        job.status = RawPayloadMigrationJob.Status.FAILED
        job.error_message = sanitize_error_message(str(exc))
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
        log.error("raw_payload_migration.unexpected_error", job_id=str(job.id))
        return

    _apply_result(job, result)
    job.refresh_from_db(fields=["cancel_requested"])

    if job.cancel_requested:
        job.status = RawPayloadMigrationJob.Status.CANCELLED
    elif job.dry_run:
        job.status = RawPayloadMigrationJob.Status.SUCCEEDED
    elif result.missing_objects or result.checksum_conflicts:
        job.status = RawPayloadMigrationJob.Status.PARTIALLY_FAILED
    else:
        job.status = RawPayloadMigrationJob.Status.SUCCEEDED

    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at", "updated_at"])
    log.info(
        "raw_payload_migration.finished",
        job_id=str(job.id),
        status=job.status,
        dry_run=job.dry_run,
        records_migrated=result.records_migrated,
    )
