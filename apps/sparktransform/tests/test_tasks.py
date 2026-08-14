import uuid
from unittest.mock import MagicMock, patch

from celery.exceptions import Retry
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.core.exceptions import SparkJobSubmissionError
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline
from apps.sparktransform.backends.base import SparkJobStatus
from apps.sparktransform.tasks import poll_spark_transform_task


class PollSparkTransformTaskTests(TenantTestCase):
    def _make_pipeline(self) -> Pipeline:
        source = Connection.objects.create(
            name="Policies API", kind=Connection.Kind.SOURCE, connector_type="rest_api", config={}
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={},
        )
        return Pipeline.objects.create(
            name="Spark pipeline",
            source_connection=source,
            destination_connection=destination,
            processing_mode=Pipeline.ProcessingMode.SPARK,
        )

    def _make_run(self, **overrides) -> PipelineRun:
        pipeline = self._make_pipeline()
        defaults = {
            "pipeline": pipeline,
            "idempotency_key": f"run-{pipeline.id}",
            "status": PipelineRun.Status.RUNNING,
            "phase": PipelineRun.Phase.TRANSFORMING,
            "spark_job_id": "job-1",
        }
        defaults.update(overrides)
        return PipelineRun.objects.create(**defaults)

    def _apply(self, run_id: str):
        return poll_spark_transform_task.apply(
            kwargs={"schema_name": self.tenant.schema_name, "run_id": run_id}
        )

    def test_skips_a_run_that_is_not_running_and_transforming(self):
        run = self._make_run(status=PipelineRun.Status.SUCCEEDED)

        with patch("apps.sparktransform.tasks.get_spark_job_backend") as mock_get_backend:
            result = self._apply(str(run.id))

        self.assertTrue(result.successful())
        mock_get_backend.assert_not_called()

    def test_skips_a_run_missing_a_spark_job_id(self):
        run = self._make_run(spark_job_id=None)

        with patch("apps.sparktransform.tasks.get_spark_job_backend") as mock_get_backend:
            result = self._apply(str(run.id))

        self.assertTrue(result.successful())
        mock_get_backend.assert_not_called()

    def test_missing_run_does_not_raise(self):
        with patch("apps.sparktransform.tasks.get_spark_job_backend") as mock_get_backend:
            result = self._apply(str(uuid.uuid4()))

        self.assertTrue(result.successful())
        mock_get_backend.assert_not_called()

    def test_non_terminal_status_retries(self):
        run = self._make_run()
        fake_backend = MagicMock()
        fake_backend.get_job_status.return_value = SparkJobStatus(
            job_id="job-1", state="RUNNING", is_terminal=False, is_success=False
        )

        with patch("apps.sparktransform.tasks.get_spark_job_backend", return_value=fake_backend):
            with self.assertRaises(Retry):
                self._apply(str(run.id))

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.RUNNING)  # not finalized yet

    def test_non_terminal_status_on_final_attempt_fails_the_run(self):
        run = self._make_run()
        fake_backend = MagicMock()
        fake_backend.get_job_status.return_value = SparkJobStatus(
            job_id="job-1", state="RUNNING", is_terminal=False, is_success=False
        )

        with patch("apps.sparktransform.tasks.get_spark_job_backend", return_value=fake_backend):
            with patch.object(poll_spark_transform_task, "max_retries", 0):
                result = self._apply(str(run.id))

        self.assertTrue(result.successful())
        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.FAILED)
        self.assertEqual(run.error_category, "SparkJobFailedError")

    def test_terminal_success_finalizes_via_finalize_spark_transform(self):
        run = self._make_run()
        job_status = SparkJobStatus(
            job_id="job-1", state="SUCCESS", is_terminal=True, is_success=True
        )
        fake_backend = MagicMock()
        fake_backend.get_job_status.return_value = job_status

        with (
            patch("apps.sparktransform.tasks.get_spark_job_backend", return_value=fake_backend),
            patch("apps.sparktransform.tasks.finalize_spark_transform") as mock_finalize,
        ):
            result = self._apply(str(run.id))

        self.assertTrue(result.successful())
        mock_finalize.assert_called_once()
        called_run, called_status = mock_finalize.call_args.args
        self.assertEqual(called_run.id, run.id)
        self.assertEqual(called_status, job_status)

    def test_retryable_backend_error_retries(self):
        run = self._make_run()
        fake_backend = MagicMock()
        fake_backend.get_job_status.side_effect = SparkJobSubmissionError("network blip")

        with patch("apps.sparktransform.tasks.get_spark_job_backend", return_value=fake_backend):
            with self.assertRaises(Retry):
                self._apply(str(run.id))

    def test_retryable_backend_error_on_final_attempt_gives_up_without_raising(self):
        run = self._make_run()
        fake_backend = MagicMock()
        fake_backend.get_job_status.side_effect = SparkJobSubmissionError("network blip")

        with patch("apps.sparktransform.tasks.get_spark_job_backend", return_value=fake_backend):
            with patch.object(poll_spark_transform_task, "max_retries", 0):
                result = self._apply(str(run.id))

        self.assertTrue(result.successful())
        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.RUNNING)  # left as-is, not force-failed
