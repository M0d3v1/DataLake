import uuid

from django.db import models

from apps.core.models import TimeStampedModel
from apps.pipelines.models import Pipeline


class PipelineRun(TimeStampedModel):
    """One execution of a Pipeline. `idempotency_key` lets retriggering
    the same logical run (e.g. a scheduler retry after a worker crash) be
    a no-op instead of a duplicate load -- see
    docs/architecture.md#idempotency.

    `status` must only be changed through `apps.execution.claims`
    (`claim_run`, `mark_run_succeeded`, `mark_run_failed`) -- those
    enforce that a terminal run (SUCCEEDED/FAILED) can never move back to
    RUNNING or PENDING. See docs/decisions/0005-execution-orchestration.md.

    `continued_from` links a run to a prior run it explicitly continues
    (via `apps.execution.dispatch.trigger_manual_run(..., continue_from=...)`)
    after that prior run failed with `PageLimitExceededError` -- a
    continuation run is seeded with the prior run's cursor/counters so it
    resumes extraction rather than re-fetching pages that already
    succeeded and loaded.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"

    class Phase(models.TextChoices):
        """Only meaningful while `status == RUNNING`, and only ever set
        for a Spark-mode pipeline (`Pipeline.processing_mode == SPARK`) --
        a plain Python-mode run's `phase` stays null throughout. See
        docs/decisions/0009-spark-backed-transform-mode.md."""

        EXTRACTING = "extracting", "Extracting"
        TRANSFORMING = "transforming", "Transforming"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    phase = models.CharField(max_length=20, choices=Phase.choices, null=True, blank=True)
    trigger = models.CharField(max_length=20, choices=Trigger.choices, default=Trigger.MANUAL)
    idempotency_key = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    continued_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="continuations",
        help_text=(
            "Set when this run was explicitly dispatched to continue a prior run that "
            "failed with PageLimitExceededError -- see apps.execution.dispatch."
        ),
    )

    # --- execution state (Milestone 2) ------------------------------------
    attempt = models.PositiveIntegerField(
        default=0, help_text="Number of claim attempts made (Celery tries + retries)."
    )
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    last_successful_cursor = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Cursor of the last page fully extracted, stored, and loaded.",
    )
    pages_extracted = models.PositiveIntegerField(default=0)
    records_extracted = models.PositiveIntegerField(default=0)
    records_loaded = models.PositiveIntegerField(default=0)
    raw_payload_count = models.PositiveIntegerField(default=0)
    # Spark mode only (see Phase above): rows the submitted job routed to
    # a dead-letter path instead of aborting the whole batch -- distinct
    # from records_extracted/records_loaded, which stay 0 for the
    # transform phase itself (Spark writes destination rows via a bulk
    # load, not one `load()` call per page like the Python-mode path).
    records_failed = models.PositiveIntegerField(default=0)
    # External Spark job identifiers (apps.sparktransform.backends) --
    # set when submit_spark_transform() dispatches this run's job,
    # updated by poll_spark_transform_task on every poll.
    spark_job_id = models.CharField(max_length=255, null=True, blank=True)
    spark_job_status = models.CharField(max_length=50, null=True, blank=True)

    # Sanitized (apps.core.redaction) -- never raw exception text that
    # might echo request/response content.
    error_category = models.CharField(max_length=100, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    error_is_retryable = models.BooleanField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PipelineRun({self.pipeline_id}, {self.status})"


class TaskExecution(TimeStampedModel):
    """One attempt at executing a PipelineRun, tracked separately from the
    run itself so retry history stays visible in execution history rather
    than being collapsed/overwritten."""

    class Step(models.TextChoices):
        EXTRACT = "extract", "Extract"
        LOAD = "load", "Load"
        RUN = "run", "Run"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="task_executions")
    step = models.CharField(max_length=20, choices=Step.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempt = models.PositiveIntegerField(default=1)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"TaskExecution({self.run_id}, {self.step}, attempt={self.attempt})"
