"""Celery execution boundary.

Deliberately thin: enter the triggering organization's tenant schema,
call the orchestration service, and translate its outcome into a Celery
retry/give-up decision. All the actual extract/store/map/load logic
lives in `apps.execution.orchestration`, which is plain Python and
testable without Celery.
"""

from celery import shared_task
from django_tenants.utils import schema_context

from apps.core.exceptions import DataLakeError
from apps.core.logging import get_logger
from apps.execution.claims import RunClaimRejected
from apps.execution.orchestration import run_pipeline
from apps.execution.retry_policy import MAX_RUN_ATTEMPTS, compute_backoff_seconds

log = get_logger(__name__)


@shared_task(bind=True, max_retries=MAX_RUN_ATTEMPTS - 1)
def run_pipeline_task(self, schema_name: str, run_id: str) -> None:
    with schema_context(schema_name):
        is_final_attempt = self.request.retries >= self.max_retries

        try:
            run_pipeline(
                run_id=run_id,
                celery_task_id=self.request.id,
                is_final_attempt=is_final_attempt,
            )
        except RunClaimRejected as exc:
            log.info("pipeline_run.claim_rejected", run_id=run_id, reason=str(exc))
            return
        except DataLakeError as exc:
            if exc.retryable and not is_final_attempt:
                countdown = compute_backoff_seconds(self.request.retries)
                log.warning(
                    "pipeline_run.retrying",
                    run_id=run_id,
                    attempt=self.request.retries + 1,
                    countdown=round(countdown, 1),
                    category=exc.category,
                )
                raise self.retry(exc=exc, countdown=countdown) from exc
            # Non-retryable, or retries exhausted: orchestration already
            # recorded this as FAILED on the run/TaskExecution -- that's
            # the system of record for execution history, not Celery's
            # own task state. Nothing further to do here but stop.
            log.error(
                "pipeline_run.gave_up",
                run_id=run_id,
                category=exc.category,
                retryable=exc.retryable,
                final_attempt=is_final_attempt,
            )
