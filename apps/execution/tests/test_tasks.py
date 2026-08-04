from unittest.mock import patch

from celery.exceptions import Retry
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.core.exceptions import ConfigurationError, FetchFailed
from apps.execution.claims import RunClaimRejected
from apps.execution.models import PipelineRun
from apps.execution.tasks import run_pipeline_task
from apps.pipelines.models import Pipeline


class RunPipelineTaskTests(TenantTestCase):
    def _make_pipeline(self) -> Pipeline:
        source = Connection.objects.create(
            name="Policies API",
            kind=Connection.Kind.SOURCE,
            connector_type="rest_api",
            config={"base_url": "https://api.example-insurer.test", "path": "/v1/policies"},
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={
                "host": "db.example-insurer.test",
                "database": "warehouse",
                "table_name": "policies",
            },
        )
        return Pipeline.objects.create(
            name="Policies sync",
            source_connection=source,
            destination_connection=destination,
            destination_mapping={"id": "id"},
        )

    def test_enters_the_correct_tenant_schema(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(pipeline=pipeline, idempotency_key="run-schema")

        seen_schema = {}

        def fake_run_pipeline(*, run_id, celery_task_id, is_final_attempt):
            from django.db import connection

            seen_schema["schema"] = connection.schema_name

        with patch("apps.execution.tasks.run_pipeline", side_effect=fake_run_pipeline):
            run_pipeline_task.apply(
                kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
            )

        self.assertEqual(seen_schema["schema"], self.tenant.schema_name)

    def test_claim_rejected_does_not_raise_or_retry(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(
            pipeline=pipeline, idempotency_key="run-terminal", status=PipelineRun.Status.SUCCEEDED
        )

        with patch(
            "apps.execution.tasks.run_pipeline", side_effect=RunClaimRejected("already succeeded")
        ):
            result = run_pipeline_task.apply(
                kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
            )

        self.assertTrue(result.successful())

    def test_retryable_failure_raises_celery_retry_when_attempts_remain(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(pipeline=pipeline, idempotency_key="run-retry")

        with patch(
            "apps.execution.tasks.run_pipeline",
            side_effect=FetchFailed("temporary", retryable=True),
        ):
            with self.assertRaises(Retry):
                run_pipeline_task.apply(
                    kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
                )

    def test_retryable_failure_gives_up_cleanly_once_attempts_are_exhausted(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(pipeline=pipeline, idempotency_key="run-exhausted")

        with patch(
            "apps.execution.tasks.run_pipeline",
            side_effect=FetchFailed("temporary", retryable=True),
        ):
            # max_retries=0: the very first (and only) attempt is already
            # final, so the task must give up rather than call self.retry().
            with patch.object(run_pipeline_task, "max_retries", 0):
                result = run_pipeline_task.apply(
                    kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
                )

        self.assertTrue(result.successful())

    def test_non_retryable_failure_does_not_retry(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(pipeline=pipeline, idempotency_key="run-nonretry")

        call_count = {"n": 0}

        def fake_run_pipeline(*, run_id, celery_task_id, is_final_attempt):
            call_count["n"] += 1
            raise ConfigurationError("bad config")

        with patch("apps.execution.tasks.run_pipeline", side_effect=fake_run_pipeline):
            run_pipeline_task.apply(
                kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
            )

        self.assertEqual(call_count["n"], 1)
