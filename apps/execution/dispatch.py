"""Application service for creating and dispatching a manual PipelineRun.

Used by the `run_pipeline` management command; a future web UI trigger
would call this same function rather than duplicating dispatch logic.
"""

import uuid

from apps.core.exceptions import ConfigurationError
from apps.execution.models import PipelineRun
from apps.execution.tasks import run_pipeline_task
from apps.pipelines.models import Pipeline

_CONTINUABLE_ERROR_CATEGORY = "PageLimitExceededError"


def trigger_manual_run(
    pipeline: Pipeline,
    *,
    schema_name: str,
    idempotency_key: str | None = None,
    continue_from: PipelineRun | None = None,
) -> tuple[PipelineRun, bool]:
    """Create (or safely reuse) a PipelineRun for `pipeline` and enqueue
    it, returning `(run, dispatched)`.

    Without an explicit `idempotency_key`, each call creates a fresh run
    (a fresh uuid-based key) -- manual triggering is "run it again" by
    default, starting extraction from the source's configured
    `start_page`. Callers that need at-most-once dispatch (e.g. a retried
    CLI invocation, or an external orchestrator wrapping this call) pass a
    deliberate, stable `idempotency_key`: a second call with the same key
    reuses the existing run and does *not* enqueue a second Celery task
    once that run is no longer PENDING (`dispatched=False`).

    `continue_from`: when a prior run failed with `PageLimitExceededError`
    (the source had more data than `max_pages` allowed, and everything up
    to the cap was already extracted, raw-stored, and loaded), pass that
    run here to resume rather than duplicate work. The new run is seeded
    with `continue_from`'s cursor and counters, so
    `apps.execution.orchestration._extract_and_load` -- which always
    resumes a run from its own `last_successful_cursor`/`pages_extracted`
    -- picks up exactly where the prior run stopped instead of
    re-fetching, re-storing, and re-loading pages that already succeeded.
    Only a run that actually failed with `PageLimitExceededError` may be
    continued: `continue_from` must belong to the same pipeline and be
    FAILED with that exact `error_category`, otherwise this raises
    `ConfigurationError` rather than silently seeding state from an
    unrelated or non-page-limit failure.

    A run is claimed exactly once and, once RUNNING, SUCCEEDED, or FAILED,
    is terminal-or-owned from a claiming standpoint (see
    `apps.execution.claims`) -- re-enqueuing against it would just be
    rejected by `claim_run`. So a FAILED run under a reused key is
    reported back as-is rather than silently re-dispatched; a genuinely
    new attempt needs a new (or omitted) `idempotency_key`.
    """
    if not pipeline.is_active:
        raise ConfigurationError(f"pipeline {pipeline.id} is not active")

    if continue_from is not None:
        if continue_from.pipeline_id != pipeline.id:
            raise ConfigurationError(
                f"cannot continue run {continue_from.id}: it belongs to a different pipeline"
            )
        if (
            continue_from.status != PipelineRun.Status.FAILED
            or continue_from.error_category != _CONTINUABLE_ERROR_CATEGORY
        ):
            raise ConfigurationError(
                f"cannot continue run {continue_from.id}: only a run FAILED with "
                f"{_CONTINUABLE_ERROR_CATEGORY!r} carries a safe resume point "
                f"(status={continue_from.status!r}, "
                f"error_category={continue_from.error_category!r})"
            )

    key = idempotency_key or f"manual-{pipeline.id}-{uuid.uuid4().hex}"

    defaults: dict = {"pipeline": pipeline, "trigger": PipelineRun.Trigger.MANUAL}
    if continue_from is not None:
        defaults.update(
            continued_from=continue_from,
            last_successful_cursor=continue_from.last_successful_cursor,
            pages_extracted=continue_from.pages_extracted,
            records_extracted=continue_from.records_extracted,
            records_loaded=continue_from.records_loaded,
            raw_payload_count=continue_from.raw_payload_count,
        )

    run, created = PipelineRun.objects.get_or_create(idempotency_key=key, defaults=defaults)

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
