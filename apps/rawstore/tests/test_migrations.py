import importlib

from django.apps import apps as global_apps
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline
from apps.rawstore.models import RawPayloadRecord

_migration_module = importlib.import_module(
    "apps.rawstore.migrations.0004_backfill_loaded_successfully"
)
backfill_loaded_successfully = _migration_module.backfill_loaded_successfully


class BackfillLoadedSuccessfullyTests(TenantTestCase):
    """Item 6 regression: `RawPayloadRecord.loaded_successfully` for rows
    that predate that field's real tracking must be corrected, not left
    at the blanket `default=False` the AddField migration gave them."""

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
            name="Policies sync", source_connection=source, destination_connection=destination
        )

    def _make_record(self, *, run_status: str, sequence: int = 1) -> RawPayloadRecord:
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(
            pipeline=pipeline, idempotency_key=f"run-{run_status}-{sequence}", status=run_status
        )
        return RawPayloadRecord.objects.create(
            run=run,
            storage_uri=f"s3://bucket/{run.id}/{sequence}",
            size_bytes=1,
            checksum_sha256="0" * 64,
            sequence=sequence,
            loaded_successfully=False,
        )

    def test_backfills_true_for_rows_belonging_to_a_succeeded_run(self):
        record = self._make_record(run_status=PipelineRun.Status.SUCCEEDED)

        backfill_loaded_successfully(global_apps, None)

        record.refresh_from_db()
        self.assertTrue(record.loaded_successfully)

    def test_leaves_rows_belonging_to_a_failed_run_unchanged(self):
        record = self._make_record(run_status=PipelineRun.Status.FAILED)

        backfill_loaded_successfully(global_apps, None)

        record.refresh_from_db()
        self.assertFalse(record.loaded_successfully)

    def test_leaves_rows_belonging_to_a_running_run_unchanged(self):
        record = self._make_record(run_status=PipelineRun.Status.RUNNING)

        backfill_loaded_successfully(global_apps, None)

        record.refresh_from_db()
        self.assertFalse(record.loaded_successfully)

    def test_does_not_touch_a_row_already_marked_loaded(self):
        record = self._make_record(run_status=PipelineRun.Status.FAILED)
        record.loaded_successfully = True
        record.save(update_fields=["loaded_successfully"])

        backfill_loaded_successfully(global_apps, None)

        record.refresh_from_db()
        self.assertTrue(record.loaded_successfully)
