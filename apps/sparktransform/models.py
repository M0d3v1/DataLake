from django.db import models

from apps.core.models import TimeStampedModel
from apps.pipelines.models import Pipeline


class SparkTransformConfig(TimeStampedModel):
    """Replaces `Pipeline.destination_mapping` for a pipeline running in
    Spark mode (`Pipeline.processing_mode == Pipeline.ProcessingMode.SPARK`)
    -- see docs/decisions/0009-spark-backed-transform-mode.md. One-to-one
    with `Pipeline`: a pipeline is either plain in-process mapping
    (default) or Spark-backed, never both.

    `column_mappings` is the same `destination_column -> source_field`
    shape as `Pipeline.destination_mapping`, plus an optional
    `transform_expr` per column:

        {
            "policy_id": {"source_field": "id", "transform_expr": None},
            "premium_usd": {
                "source_field": "premium",
                "transform_expr": "CAST(premium AS DECIMAL)",
            },
        }

    Shape and grammar are validated by
    `apps.sparktransform.services.validate_spark_transform_config` --
    not automatically on every `save()` (this codebase has no
    pipeline-config form/API yet for that hook to attach to; see that
    function's docstring), but always before a Spark job is submitted.
    """

    pipeline = models.OneToOneField(
        Pipeline, on_delete=models.CASCADE, related_name="spark_transform_config"
    )
    column_mappings = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"SparkTransformConfig({self.pipeline_id})"
