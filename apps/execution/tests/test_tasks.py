from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.execution.tasks import run_pipeline_stub
from apps.pipelines.models import Pipeline


class RunPipelineStubTaskTests(TenantTestCase):
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
            name="Policies sync", source_connection=source, destination_connection=destination
        )

    def test_task_marks_pending_run_succeeded(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(pipeline=pipeline, idempotency_key="run-1")

        run_pipeline_stub.apply(
            kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
        )

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)

    def test_task_skips_a_run_that_is_not_pending(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(
            pipeline=pipeline, idempotency_key="run-2", status=PipelineRun.Status.SUCCEEDED
        )

        run_pipeline_stub.apply(
            kwargs={"schema_name": self.tenant.schema_name, "run_id": str(run.id)}
        )

        run.refresh_from_db()
        self.assertIsNone(run.started_at)
