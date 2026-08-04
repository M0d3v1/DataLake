import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
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
