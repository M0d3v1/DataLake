import uuid

from django.db import models

from apps.core.models import TimeStampedModel
from apps.pipelines.models import Pipeline


class PipelineRun(TimeStampedModel):
    """One execution of a Pipeline. `idempotency_key` lets retriggering
    the same logical run (e.g. a scheduler retry after a worker crash) be
    a no-op instead of a duplicate load -- see
    docs/architecture.md#idempotency."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    trigger = models.CharField(max_length=20, choices=Trigger.choices, default=Trigger.MANUAL)
    idempotency_key = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PipelineRun({self.pipeline_id}, {self.status})"


class TaskExecution(TimeStampedModel):
    """One step (extract/load/...) within a PipelineRun, tracked
    separately so partial failures and per-step retries are visible in
    execution history rather than collapsed into the run as a whole."""

    class Step(models.TextChoices):
        EXTRACT = "extract", "Extract"
        LOAD = "load", "Load"

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
