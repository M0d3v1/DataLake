"""Pluggable backend for submitting and polling a Spark transform job --
the same `ABC` + settings-driven `import_string` singleton pattern
`apps.rawstore.base.RawPayloadStore` and `apps.secrets.base.SecretStore`
already use, so which serverless Spark platform a deployment targets
(AWS EMR Serverless, Databricks Jobs API, GCP Dataproc Serverless -- see
docs/decisions/0009-spark-backed-transform-mode.md) is a settings choice,
not something load-bearing in calling code.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class SparkJobStatus:
    """A snapshot of an external Spark job's state.

    `state` is the backend's own native status string (e.g. EMR
    Serverless's `RUNNING`/`SUCCESS`/`FAILED`/`CANCELLED`), kept
    verbatim in `PipelineRun.spark_job_status` for visibility rather
    than collapsed into this platform's own vocabulary. `is_terminal`/
    `is_success` are what `apps.sparktransform.tasks.poll_spark_transform_task`
    actually branches on, so that translation lives in exactly one place
    per backend, not scattered through the polling task.
    """

    job_id: str
    state: str
    is_terminal: bool
    is_success: bool
    error_message: str | None = None


class SparkJobBackend(ABC):
    @abstractmethod
    def submit_job(self, *, input_prefix: str, output_prefix: str, job_config: dict) -> str:
        """Submit a Spark transform job reading raw payloads from
        `input_prefix` and writing destination-shaped output to
        `output_prefix` (both object-storage URI prefixes -- see
        `apps.sparktransform.services.submit_spark_transform`).
        `job_config` is the precompiled, whitelisted column-mapping
        payload (`{destination_column: {"source_field": ..., "spark_sql_expr":
        ...}}` -- see `apps.sparktransform.expr.compile_to_spark_sql`) the
        job itself trusts and runs, never re-parsing tenant-supplied
        `transform_expr` text.

        Returns an opaque job id to poll later. Raises
        `apps.core.exceptions.SparkJobSubmissionError` (retryable) on any
        failure to submit -- a transient backend/network problem, not a
        job that was accepted and then failed on its own merits."""

    @abstractmethod
    def get_job_status(self, job_id: str) -> SparkJobStatus:
        """Poll `job_id`'s current status. Raises
        `apps.core.exceptions.SparkJobSubmissionError` (retryable) if the
        backend itself couldn't be reached -- a job that was reached and
        reports FAILED is not an exception here, it's a terminal,
        non-success `SparkJobStatus`."""


_backend_instance: SparkJobBackend | None = None


def get_spark_job_backend() -> SparkJobBackend:
    global _backend_instance
    if _backend_instance is None:
        backend_cls = import_string(settings.SPARK_JOB_BACKEND)
        _backend_instance = backend_cls()
    return _backend_instance
