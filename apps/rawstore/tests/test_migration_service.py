import hashlib
from unittest.mock import MagicMock, patch

import botocore.exceptions
from django.conf import settings
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.core.exceptions import ConfigurationError
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline
from apps.rawstore.backends.s3 import S3RawPayloadStore
from apps.rawstore.migration import run_raw_payload_migration
from apps.rawstore.models import RawPayloadRecord


def _client_error(code: str, operation: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _body(data: bytes) -> MagicMock:
    body = MagicMock()
    body.read.return_value = data
    return body


class RawPayloadMigrationServiceTests(TenantTestCase):
    def _store_with_mock_client(self):
        with patch("apps.rawstore.backends.s3.boto3.client") as mock_boto_client:
            store = S3RawPayloadStore()
        return store, mock_boto_client.return_value

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

    def _make_run(self) -> PipelineRun:
        pipeline = self._make_pipeline()
        return PipelineRun.objects.create(
            pipeline=pipeline, idempotency_key=f"run-{RawPayloadRecord.objects.count()}"
        )

    def _make_record(
        self, run: PipelineRun, *, sequence: int, data: bytes, uri: str
    ) -> RawPayloadRecord:
        return RawPayloadRecord.objects.create(
            run=run,
            storage_uri=uri,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            sequence=sequence,
        )

    def _logical_key(self, run: PipelineRun, sequence: int, data: bytes) -> str:
        checksum = hashlib.sha256(data).hexdigest()
        return f"{run.pipeline_id}/{run.id}/{sequence:06d}-{checksum}.raw"

    def _run(self, store, **kwargs):
        with patch("apps.rawstore.migration.get_raw_payload_store", return_value=store):
            return run_raw_payload_migration(**kwargs)

    def test_rejects_a_non_s3_backend(self):
        with patch("apps.rawstore.migration.get_raw_payload_store", return_value=object()):
            with self.assertRaises(ConfigurationError):
                run_raw_payload_migration(dry_run=True)

    def test_already_migrated_record_is_counted_and_left_untouched(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-1"
        logical = self._logical_key(run, 1, data)
        uri = f"s3://{settings.RAW_STORE_BUCKET}/{self.tenant.tenant_uuid}/{logical}"
        self._make_record(run, sequence=1, data=data, uri=uri)

        result = self._run(store, dry_run=True)

        self.assertEqual(result.records_inspected, 1)
        self.assertEqual(result.already_migrated, 1)
        self.assertEqual(result.ready_for_migration, 0)
        mock_client.get_object.assert_not_called()

    def test_legacy_unprefixed_record_dry_run_does_not_mutate(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-legacy"
        logical = self._logical_key(run, 1, data)
        uri = f"s3://{settings.RAW_STORE_BUCKET}/{logical}"
        record = self._make_record(run, sequence=1, data=data, uri=uri)
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(data),
            "ContentType": "application/json",
        }

        result = self._run(store, dry_run=True)

        self.assertEqual(result.legacy_unprefixed, 1)
        self.assertEqual(result.ready_for_migration, 1)
        self.assertEqual(result.records_migrated, 0)
        record.refresh_from_db()
        self.assertEqual(record.storage_uri, uri)  # unchanged
        mock_client.put_object.assert_not_called()

    def test_legacy_unprefixed_record_real_run_migrates_and_updates_storage_uri(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-legacy-real"
        logical = self._logical_key(run, 1, data)
        record = self._make_record(
            run, sequence=1, data=data, uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical}"
        )
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(data),
            "ContentType": "application/json",
        }

        result = self._run(store, dry_run=False)

        self.assertEqual(result.records_migrated, 1)
        record.refresh_from_db()
        self.assertTrue(
            record.storage_uri.startswith(
                f"s3://{settings.RAW_STORE_BUCKET}/{self.tenant.tenant_uuid}/"
            )
        )
        put_kwargs = mock_client.put_object.call_args.kwargs
        self.assertEqual(put_kwargs["IfNoneMatch"], "*")

    def test_schema_prefixed_record_is_classified_and_migrated(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-schema-prefixed"
        logical = self._logical_key(run, 1, data)
        uri = f"s3://{settings.RAW_STORE_BUCKET}/{self.tenant.schema_name}/{logical}"
        record = self._make_record(run, sequence=1, data=data, uri=uri)
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(data),
            "ContentType": "application/json",
        }

        result = self._run(store, dry_run=False)

        self.assertEqual(result.schema_prefixed, 1)
        self.assertEqual(result.records_migrated, 1)
        record.refresh_from_db()
        self.assertIn(str(self.tenant.tenant_uuid), record.storage_uri)

    def test_missing_object_is_reported_and_not_migrated(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-missing"
        logical = self._logical_key(run, 1, data)
        self._make_record(
            run, sequence=1, data=data, uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical}"
        )
        mock_client.head_object.side_effect = _client_error("404", "HeadObject")

        result = self._run(store, dry_run=True)

        self.assertEqual(result.missing_objects, 1)
        self.assertEqual(result.ready_for_migration, 0)
        self.assertEqual(len(result.problem_records), 1)
        self.assertEqual(result.problem_records[0]["category"], "missing")

    def test_checksum_conflict_on_downloaded_bytes_is_reported_and_not_migrated(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"expected-bytes"
        logical = self._logical_key(run, 1, data)
        self._make_record(
            run, sequence=1, data=data, uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical}"
        )
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(b"tampered-or-corrupted-bytes"),
            "ContentType": "application/json",
        }

        result = self._run(store, dry_run=True)

        self.assertEqual(result.checksum_conflicts, 1)
        self.assertEqual(result.ready_for_migration, 0)
        self.assertEqual(result.problem_records[0]["category"], "checksum_conflict")

    def test_unrecognized_key_shape_is_a_checksum_conflict_not_a_crash(self):
        store, _mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-x"
        self._make_record(run, sequence=1, data=data, uri="s3://some-other-bucket/whatever/key.raw")

        result = self._run(store, dry_run=True)

        self.assertEqual(result.checksum_conflicts, 1)
        self.assertEqual(result.records_inspected, 1)

    def test_progress_callback_is_invoked(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data = b"payload-cb"
        logical = self._logical_key(run, 1, data)
        self._make_record(
            run, sequence=1, data=data, uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical}"
        )
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(data),
            "ContentType": "application/json",
        }

        calls = []
        self._run(
            store,
            dry_run=True,
            batch_size=1,
            progress_callback=lambda r: calls.append(r.records_inspected),
        )

        self.assertTrue(calls)
        self.assertEqual(calls[-1], 1)

    def test_cancel_check_stops_the_loop_early(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        data1 = b"payload-a"
        data2 = b"payload-b"
        logical1 = self._logical_key(run, 1, data1)
        logical2 = self._logical_key(run, 2, data2)
        self._make_record(
            run, sequence=1, data=data1, uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical1}"
        )
        self._make_record(
            run, sequence=2, data=data2, uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical2}"
        )
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(data1),
            "ContentType": "application/json",
        }

        result = self._run(store, dry_run=True, batch_size=1, cancel_check=lambda: True)

        self.assertEqual(result.records_inspected, 1)

    def test_records_skipped_aggregates_non_migrated_categories(self):
        store, mock_client = self._store_with_mock_client()
        run = self._make_run()
        already_data = b"already"
        already_logical = self._logical_key(run, 1, already_data)
        self._make_record(
            run,
            sequence=1,
            data=already_data,
            uri=f"s3://{settings.RAW_STORE_BUCKET}/{self.tenant.tenant_uuid}/{already_logical}",
        )
        missing_data = b"missing"
        missing_logical = self._logical_key(run, 2, missing_data)
        self._make_record(
            run,
            sequence=2,
            data=missing_data,
            uri=f"s3://{settings.RAW_STORE_BUCKET}/{missing_logical}",
        )
        mock_client.head_object.side_effect = _client_error("404", "HeadObject")

        result = self._run(store, dry_run=True)

        self.assertEqual(result.records_skipped, 2)  # already_migrated(1) + missing(1)
