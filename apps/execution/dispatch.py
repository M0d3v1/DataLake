"""Application service for creating and dispatching a manual PipelineRun.

Used by the `run_pipeline` management command; a future web UI trigger
would call this same function rather than duplicating dispatch logic.
"""

import uuid

from apps.core.exceptions import ConfigurationError
from apps.execution.models import PipelineRun
from apps.execution.tasks import run_pipeline_task
from apps.pipelines.models import Pipeline


def trigger_manual_run(
    pipeline: Pipeline, *, schema_name: str, idempotency_key: str | None = None
) -> tuple[PipelineRun, bool]:
    """Create (or safely reuse) a PipelineRun for `pipeline` and enqueue
    it, returning `(run, dispatched)`.

    Without an explicit `idempotency_key`, each call creates a fresh run
    (a fresh uuid-based key) -- manual triggering is "run it again" by
    default. Callers that need at-most-once dispatch (e.g. a retried CLI
    invocation, or an external orchestrator wrapping this call) pass a
    deliberate, stable `idempotency_key`: a second call with the same key
    reuses the existing run and does *not* enqueue a second Celery task
    once that run is no longer PENDING (`dispatched=False`).

    A run is claimed exactly once and, once RUNNING, SUCCEEDED, or FAILED,
    is terminal-or-owned from a claiming standpoint (see
    `apps.execution.claims`) -- re-enqueuing against it would just be
    rejected by `claim_run`. So a FAILED run under a reused key is
    reported back as-is rather than silently re-dispatched; a genuinely
    new attempt needs a new (or omitted) `idempotency_key`.
    """
    if not pipeline.is_active:
        raise ConfigurationError(f"pipeline {pipeline.id} is not active")

    key = idempotency_key or f"manual-{pipeline.id}-{uuid.uuid4().hex}"

    run, created = PipelineRun.objects.get_or_create(
        idempotency_key=key,
        defaults={"pipeline": pipeline, "trigger": PipelineRun.Trigger.MANUAL},
    )

    if not created:
        if run.pipeline_id != pipeline.id:
            raise ConfigurationError(
                f"idempotency_key {key!r} is already in use by a run of a different pipeline"
            )
        if run.status != PipelineRun.Status.PENDING:
            return run, False
        # status is still PENDING: the previous call's .delay() may never
        # have reached the broker (process crash between create and
        # enqueue) -- safe, and necessary, to enqueue it (again).

    run_pipeline_task.delay(schema_name=schema_name, run_id=str(run.id))
    return run, True
