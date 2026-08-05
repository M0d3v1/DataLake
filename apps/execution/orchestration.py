"""The real extract -> raw-store -> map -> load pipeline execution flow.

This is a service module, not a Celery task: `apps.execution.tasks` is a
thin boundary that enters the tenant schema and translates the outcome
into Celery retry/give-up decisions. Everything about *what a pipeline
run does* lives here so it's directly unit-testable without Celery, a
broker, or a real tenant schema switch.
"""

from typing import Any

from django.utils import timezone

from apps.connections.models import Connection, Credential
from apps.connectors.registry import get_destination_connector, get_source_connector
from apps.core.exceptions import DataLakeError, MappingError
from apps.core.logging import get_logger
from apps.core.redaction import sanitize_error_message
from apps.execution.claims import (
    claim_run,
    mark_run_failed,
    mark_run_succeeded,
    record_pending_retry,
)
from apps.execution.models import PipelineRun, TaskExecution
from apps.pipelines.mapping import map_records
from apps.rawstore.service import store_raw_payload

log = get_logger(__name__)


def run_pipeline(*, run_id: str, celery_task_id: str | None, is_final_attempt: bool) -> None:
    """Claim `run_id`, execute it fully, and record the outcome.

    Raises `apps.execution.claims.RunClaimRejected` if the run couldn't
    be claimed (already terminal, or owned by a different in-flight
    execution) -- callers should treat that as "nothing to do", not an
    error. Re-raises the original `DataLakeError` on any execution
    failure, with `.retryable` set correctly, after having already
    persisted the failure onto the run/TaskExecution -- see
    `apps.execution.tasks.run_pipeline_task` for how that's turned into a
    Celery retry-or-give-up decision.
    """
    run = claim_run(run_id, celery_task_id)

    task_execution = TaskExecution.objects.create(
        run=run,
        step=TaskExecution.Step.RUN,
        status=TaskExecution.Status.RUNNING,
        attempt=run.attempt,
        started_at=timezone.now(),
    )
    log.info(
        "pipeline_run.attempt_started",
        run_id=str(run.id),
        pipeline_id=str(run.pipeline_id),
        attempt=run.attempt,
        is_final_attempt=is_final_attempt,
    )

    try:
        _execute(run)
    except DataLakeError as exc:
        _record_failure(run, task_execution, exc, is_final_attempt=is_final_attempt)
        raise
    except Exception as exc:  # noqa: BLE001 -- convert unclassified errors
        # into a non-retryable failure rather than let a genuine bug
        # retry forever (or crash the worker process without recording
        # anything in execution history).
        wrapped = DataLakeError(str(exc), retryable=False, category="UnexpectedError")
        _record_failure(run, task_execution, wrapped, is_final_attempt=is_final_attempt)
        raise wrapped from exc
    else:
        _record_success(run, task_execution)


def _execute(run: PipelineRun) -> None:
    pipeline = run.pipeline
    source_connection = pipeline.source_connection
    destination_connection = pipeline.destination_connection

    mapping = pipeline.destination_mapping
    if not mapping:
        raise MappingError(f"pipeline {pipeline.id} has no destination_mapping configured")

    source_config = {**source_connection.config, **pipeline.extraction_config}
    source = get_source_connector(source_connection.connector_type, source_config)
    destination = get_destination_connector(
        destination_connection.connector_type, destination_connection.config
    )

    # Credentials are resolved only right before the phase that needs
    # them, held only for the duration of this function, and never
    # written to the run/log -- see docs/decisions/0005-execution-orchestration.md.
    source_credential = _resolve_credential(source_connection)
    destination_credential = _resolve_credential(destination_connection)
    try:
        _extract_and_load(
            run,
            source,
            destination,
            mapping,
            pipeline.strict_mapping,
            source_credential,
            destination_credential,
        )
    finally:
        del source_credential
        del destination_credential


def _extract_and_load(
    run: PipelineRun,
    source,
    destination,
    mapping: dict[str, str],
    strict_mapping: bool,
    source_credential: dict[str, Any],
    destination_credential: dict[str, Any],
) -> None:
    # Resume from the last successful page/cursor -- a retry after a
    # mid-run failure re-extracts nothing that already succeeded.
    cursor = run.last_successful_cursor
    sequence = run.pages_extracted

    for page in source.fetch(source_credential, cursor=cursor):
        sequence += 1

        raw_record = store_raw_payload(
            run,
            sequence=sequence,
            data=page.raw_payload,
            content_type=page.raw_content_type,
            cursor_used=page.cursor_used,
            next_cursor=page.next_cursor,
            item_count=len(page.records),
            source_path=page.source_path or "",
            http_status=page.http_status,
        )

        mapped_records = map_records(page.records, mapping, strict=strict_mapping)
        loaded_count = destination.load(destination_credential, mapped_records, mode="append")

        # Only mark the *version we actually acted on* as loaded -- see
        # RawPayloadRecord.loaded_successfully and
        # docs/decisions/0007-raw-payload-immutability.md.
        raw_record.loaded_successfully = True
        raw_record.save(update_fields=["loaded_successfully", "updated_at"])

        run.pages_extracted = sequence
        run.records_extracted += len(page.records)
        run.records_loaded += loaded_count
        run.raw_payload_count += 1
        run.last_successful_cursor = page.next_cursor or page.cursor_used
        run.save(
            update_fields=[
                "pages_extracted",
                "records_extracted",
                "records_loaded",
                "raw_payload_count",
                "last_successful_cursor",
                "updated_at",
            ]
        )
        log.info(
            "pipeline_run.page_processed",
            run_id=str(run.id),
            sequence=sequence,
            records_extracted=len(page.records),
            records_loaded=loaded_count,
        )


def _resolve_credential(connection: Connection) -> dict[str, Any]:
    try:
        credential_obj = connection.credential
    except Credential.DoesNotExist:
        return {}
    return credential_obj.resolve()


def _record_success(run: PipelineRun, task_execution: TaskExecution) -> None:
    now = timezone.now()
    mark_run_succeeded(run)
    task_execution.status = TaskExecution.Status.SUCCEEDED
    task_execution.finished_at = now
    task_execution.save(update_fields=["status", "finished_at", "updated_at"])
    log.info(
        "pipeline_run.succeeded",
        run_id=str(run.id),
        pages_extracted=run.pages_extracted,
        records_extracted=run.records_extracted,
        records_loaded=run.records_loaded,
    )


def _record_failure(
    run: PipelineRun, task_execution: TaskExecution, exc: DataLakeError, *, is_final_attempt: bool
) -> None:
    sanitized_message = sanitize_error_message(str(exc))
    should_finalize = is_final_attempt or not exc.retryable

    if should_finalize:
        mark_run_failed(
            run, category=exc.category, message=sanitized_message, retryable=exc.retryable
        )
    else:
        record_pending_retry(
            run, category=exc.category, message=sanitized_message, retryable=exc.retryable
        )

    task_execution.status = (
        TaskExecution.Status.FAILED if should_finalize else TaskExecution.Status.RUNNING
    )
    task_execution.error_message = sanitized_message
    update_fields = ["status", "error_message", "updated_at"]
    if should_finalize:
        task_execution.finished_at = timezone.now()
        update_fields.append("finished_at")
    task_execution.save(update_fields=update_fields)

    log.error(
        "pipeline_run.attempt_failed",
        run_id=str(run.id),
        category=exc.category,
        retryable=exc.retryable,
        will_retry=not should_finalize,
    )
