"""Celery polling boundary for a submitted Spark transform job -- the
same shape as `apps.execution.tasks.run_pipeline_task`: deliberately
thin, enters the triggering organization's tenant schema, and translates
the backend's job status into a Celery retry/give-up/finalize decision.
All the actual logic (reading job status, bulk-loading output, marking
the run succeeded/failed) lives in `apps.sparktransform.services`, which
is plain Python and testable without Celery.
"""

from celery import shared_task
from django_tenants.utils import schema_context

from apps.core.exceptions import DataLakeError
from apps.core.logging import get_logger
from apps.execution.claims import mark_run_failed
from apps.execution.models import PipelineRun
from apps.sparktransform.backends.base import get_spark_job_backend
from apps.sparktransform.poll_policy import MAX_POLL_ATTEMPTS, compute_poll_backoff_seconds
from apps.sparktransform.services import finalize_spark_transform

log = get_logger(__name__)


@shared_task(bind=True, max_retries=MAX_POLL_ATTEMPTS - 1)
def poll_spark_transform_task(self, schema_name: str, run_id: str) -> None:
    with schema_context(schema_name):
        try:
            run = PipelineRun.objects.select_related(
                "pipeline", "pipeline__destination_connection"
            ).get(pk=run_id)
        except PipelineRun.DoesNotExist:
            log.error("spark_transform.poll_run_missing", run_id=run_id)
            return

        # A run that isn't RUNNING/TRANSFORMING anymore was already
        # finalized by an earlier poll (or something else entirely) --
        # nothing to do, not an error. Mirrors
        # apps.execution.claims.RunClaimRejected's "already handled"
        # posture for the run_pipeline_task boundary.
        if run.status != PipelineRun.Status.RUNNING or run.phase != PipelineRun.Phase.TRANSFORMING:
            log.info(
                "spark_transform.poll_skipped",
                run_id=run_id,
                status=run.status,
                phase=run.phase,
            )
            return
        if not run.spark_job_id:
            log.error("spark_transform.poll_missing_job_id", run_id=run_id)
            return

        is_final_attempt = self.request.retries >= self.max_retries
        backend = get_spark_job_backend()

        try:
            job_status = backend.get_job_status(run.spark_job_id)
        except DataLakeError as exc:
            if exc.retryable and not is_final_attempt:
                countdown = compute_poll_backoff_seconds(self.request.retries)
                log.warning(
                    "spark_transform.poll_retrying",
                    run_id=run_id,
                    attempt=self.request.retries + 1,
                    countdown=round(countdown, 1),
                    category=exc.category,
                )
                raise self.retry(exc=exc, countdown=countdown) from exc
            log.error(
                "spark_transform.poll_gave_up",
                run_id=run_id,
                category=exc.category,
                final_attempt=is_final_attempt,
            )
            return

        if not job_status.is_terminal:
            if is_final_attempt:
                log.error(
                    "spark_transform.poll_timed_out",
                    run_id=run_id,
                    job_id=run.spark_job_id,
                    max_attempts=MAX_POLL_ATTEMPTS,
                )
                mark_run_failed(
                    run,
                    category="SparkJobFailedError",
                    message=(
                        f"Spark job {run.spark_job_id} did not reach a terminal state "
                        f"within {MAX_POLL_ATTEMPTS} polling attempts"
                    ),
                    retryable=False,
                )
                return
            countdown = compute_poll_backoff_seconds(self.request.retries)
            raise self.retry(countdown=countdown)

        finalize_spark_transform(run, job_status)
