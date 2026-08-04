"""Celery execution skeleton.

`run_pipeline_stub` demonstrates the shape every real pipeline task will
follow: switch into the triggering organization's tenant schema, load the
PipelineRun, transition its status, and log structured events correlated
by `run_id`. It does not yet perform real extract -> raw-store -> load
orchestration -- that's Milestone 2 (`apps.connectors`, `apps.rawstore`,
and the auth/connector interfaces built in Milestone 1 are the pieces it
will be assembled from).
"""

from celery import shared_task
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.core.logging import get_logger

log = get_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def run_pipeline_stub(self, schema_name: str, run_id: str) -> None:
    from apps.execution.models import PipelineRun

    with schema_context(schema_name):
        run = PipelineRun.objects.select_related("pipeline").get(id=run_id)

        if run.status != PipelineRun.Status.PENDING:
            log.info(
                "pipeline_run.skipped_not_pending",
                run_id=str(run.id),
                status=run.status,
            )
            return

        run.status = PipelineRun.Status.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])
        log.info(
            "pipeline_run.started",
            run_id=str(run.id),
            pipeline_id=str(run.pipeline_id),
            schema=schema_name,
        )

        try:
            # Milestone 2: real extract -> store-raw -> map -> load chain.
            run.status = PipelineRun.Status.SUCCEEDED
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at", "updated_at"])
            log.info("pipeline_run.succeeded", run_id=str(run.id))
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any
            # extract/load failure must land the run in FAILED with a
            # message, not bubble past this boundary unrecorded.
            run.status = PipelineRun.Status.FAILED
            run.error_message = str(exc)
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
            log.error("pipeline_run.failed", run_id=str(run.id), error=str(exc))
            raise
