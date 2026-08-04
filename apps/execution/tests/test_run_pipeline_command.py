import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline


class RunPipelineCommandTests(TenantTestCase):
    def _make_pipeline(self, **overrides) -> Pipeline:
        source = Connection.objects.create(
            name="Policies API", kind=Connection.Kind.SOURCE, connector_type="rest_api", config={}
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={},
        )
        defaults = {
            "name": "Policies sync",
            "source_connection": source,
            "destination_connection": destination,
            "destination_mapping": {"id": "id"},
        }
        defaults.update(overrides)
        return Pipeline.objects.create(**defaults)

    def test_dispatches_a_run_and_prints_only_safe_identifiers(self):
        pipeline = self._make_pipeline()
        out = StringIO()

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            call_command(
                "run_pipeline", str(pipeline.id), "--schema", self.tenant.schema_name, stdout=out
            )

        output = out.getvalue()
        self.assertIn(str(pipeline.id), output)
        self.assertIn(self.tenant.schema_name, output)
        mock_task.delay.assert_called_once()

    def test_unknown_pipeline_id_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("run_pipeline", str(uuid.uuid4()), "--schema", self.tenant.schema_name)

    def test_malformed_pipeline_id_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("run_pipeline", "not-a-uuid", "--schema", self.tenant.schema_name)

    def test_inactive_pipeline_raises_command_error(self):
        pipeline = self._make_pipeline(is_active=False)

        with self.assertRaises(CommandError):
            call_command("run_pipeline", str(pipeline.id), "--schema", self.tenant.schema_name)

    def test_continue_from_dispatches_a_run_seeded_with_the_failed_runs_cursor(self):
        pipeline = self._make_pipeline()
        failed_run = PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key="failed-run",
            status=PipelineRun.Status.FAILED,
            error_category="PageLimitExceededError",
            last_successful_cursor="4",
            pages_extracted=3,
        )
        out = StringIO()

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            call_command(
                "run_pipeline",
                str(pipeline.id),
                "--schema",
                self.tenant.schema_name,
                "--continue-from",
                str(failed_run.id),
                stdout=out,
            )

        mock_task.delay.assert_called_once()
        new_run = PipelineRun.objects.exclude(id=failed_run.id).get(pipeline=pipeline)
        self.assertEqual(new_run.continued_from_id, failed_run.id)
        self.assertEqual(new_run.last_successful_cursor, "4")
        self.assertIn(str(failed_run.id), out.getvalue())

    def test_continue_from_unknown_run_id_raises_command_error(self):
        pipeline = self._make_pipeline()

        with self.assertRaises(CommandError):
            call_command(
                "run_pipeline",
                str(pipeline.id),
                "--schema",
                self.tenant.schema_name,
                "--continue-from",
                str(uuid.uuid4()),
            )

    def test_continue_from_a_run_that_did_not_fail_with_page_limit_raises_command_error(self):
        pipeline = self._make_pipeline()
        succeeded_run = PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key="succeeded-run",
            status=PipelineRun.Status.SUCCEEDED,
        )

        with self.assertRaises(CommandError):
            call_command(
                "run_pipeline",
                str(pipeline.id),
                "--schema",
                self.tenant.schema_name,
                "--continue-from",
                str(succeeded_run.id),
            )
