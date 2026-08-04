"""Concurrency-safe claiming of a PipelineRun, and the only place
`PipelineRun.status` is mutated -- see
docs/decisions/0005-execution-orchestration.md.

A completed run (SUCCEEDED/FAILED) is terminal: nothing here will ever
move it back to RUNNING or PENDING.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import DataLakeError
from apps.execution.models import PipelineRun

TERMINAL_STATUSES = {PipelineRun.Status.SUCCEEDED, PipelineRun.Status.FAILED}


class RunClaimRejected(DataLakeError):
    """Not a failure -- the run is already finished, or a different
    (still-owning) execution has it. Callers should treat this as
    "nothing to do", never retry or fail the run over it."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="RunClaimRejected")


class InvalidRunTransition(DataLakeError):
    """An internal consistency error: something tried to finalize a run
    that wasn't RUNNING. This should be unreachable in normal operation
    (claim_run is the only entry point into RUNNING) -- if it happens,
    it's a bug in the caller, not a transient condition."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="InvalidRunTransition")


def claim_run(run_id: str, celery_task_id: str | None) -> PipelineRun:
    """Atomically claim `run_id` for execution.

    Uses `SELECT ... FOR UPDATE SKIP LOCKED` so two workers racing to
    claim the same run never block each other -- the loser sees no row
    (skipped, still locked by the winner's open transaction) and raises
    immediately rather than waiting and then double-executing.

    A run already RUNNING is only re-claimable by the *same* Celery task
    id -- this is what makes `self.retry()` (which Celery re-invokes
    under the same root task id) a legitimate continuation, while a
    genuinely concurrent second dispatch (a different task id) is
    rejected instead of executing the same run twice. This relies on
    Celery preserving the task id across a retry chain; see the ADR for
    the known limitation this implies (a hard-killed worker, as opposed
    to a clean `self.retry()`, leaves the run stuck RUNNING with no
    automatic recovery in this milestone).
    """
    with transaction.atomic():
        run = (
            PipelineRun.objects.select_for_update(skip_locked=True)
            .select_related(
                "pipeline", "pipeline__source_connection", "pipeline__destination_connection"
            )
            .filter(pk=run_id)
            .first()
        )
        if run is None:
            raise RunClaimRejected(
                f"pipeline run {run_id} was not found, or is currently locked by another claim"
            )
        if run.status in TERMINAL_STATUSES:
            raise RunClaimRejected(f"pipeline run {run_id} is already {run.status}")
        if run.status == PipelineRun.Status.RUNNING and run.celery_task_id != celery_task_id:
            raise RunClaimRejected(
                f"pipeline run {run_id} is already being executed "
                f"(owning task {run.celery_task_id!r})"
            )

        if run.status == PipelineRun.Status.PENDING:
            run.started_at = run.started_at or timezone.now()
        run.status = PipelineRun.Status.RUNNING
        run.celery_task_id = celery_task_id
        run.attempt = run.attempt + 1
        run.save(update_fields=["status", "started_at", "celery_task_id", "attempt", "updated_at"])
        return run


def mark_run_succeeded(run: PipelineRun) -> None:
    if run.status != PipelineRun.Status.RUNNING:
        raise InvalidRunTransition(f"cannot mark run {run.id} succeeded from status {run.status}")
    run.status = PipelineRun.Status.SUCCEEDED
    run.finished_at = timezone.now()
    run.error_category = None
    run.error_message = None
    run.error_is_retryable = None
    run.save(
        update_fields=[
            "status",
            "finished_at",
            "error_category",
            "error_message",
            "error_is_retryable",
            "updated_at",
        ]
    )


def mark_run_failed(run: PipelineRun, *, category: str, message: str, retryable: bool) -> None:
    if run.status != PipelineRun.Status.RUNNING:
        raise InvalidRunTransition(f"cannot mark run {run.id} failed from status {run.status}")
    run.status = PipelineRun.Status.FAILED
    run.finished_at = timezone.now()
    run.error_category = category
    run.error_message = message
    run.error_is_retryable = retryable
    run.save(
        update_fields=[
            "status",
            "finished_at",
            "error_category",
            "error_message",
            "error_is_retryable",
            "updated_at",
        ]
    )


def record_pending_retry(run: PipelineRun, *, category: str, message: str, retryable: bool) -> None:
    """A retryable failure that hasn't exhausted its attempts yet:
    persist the latest error info for visibility in execution history,
    but leave `status` as RUNNING -- Celery will re-invoke the task."""
    run.error_category = category
    run.error_message = message
    run.error_is_retryable = retryable
    run.save(update_fields=["error_category", "error_message", "error_is_retryable", "updated_at"])
