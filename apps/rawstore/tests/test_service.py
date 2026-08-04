import hashlib
from unittest.mock import MagicMock, patch

from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline
from apps.rawstore.models import RawPayloadRecord
from apps.rawstore.service import store_raw_payload


class StoreRawPayloadTests(TenantTestCase):
    def _make_run(self) -> PipelineRun:
        source = Connection.objects.create(
            name="Policies API", kind=Connection.Kind.SOURCE, connector_type="rest_api", config={}
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={},
        )
        pipeline = Pipeline.objects.create(
            name="Policies sync", source_connection=source, destination_connection=destination
        )
        return PipelineRun.objects.create(pipeline=pipeline, idempotency_key="raw-run-1")

    def test_store_raw_payload_persists_metadata_and_delegates_to_backend(self):
        run = self._make_run()
        payload = b'{"policy_id": "P-1"}'
        fake_store = MagicMock()
        fake_store.put.return_value = "s3://bucket/key"

        with patch("apps.rawstore.service.get_raw_payload_store", return_value=fake_store):
            record = store_raw_payload(
                run, sequence=1, data=payload, content_type="application/json"
            )

        fake_store.put.assert_called_once()
        self.assertEqual(record.storage_uri, "s3://bucket/key")
        self.assertEqual(record.size_bytes, len(payload))
        self.assertEqual(record.sequence, 1)
        self.assertEqual(record.checksum_sha256, hashlib.sha256(payload).hexdigest())

    def test_identical_retry_reuses_the_existing_object_and_row(self):
        run = self._make_run()
        fake_store = MagicMock()
        fake_store.put.return_value = "s3://bucket/key"

        with patch("apps.rawstore.service.get_raw_payload_store", return_value=fake_store):
            first = store_raw_payload(
                run, sequence=1, data=b"identical-bytes", content_type="application/json"
            )
            second = store_raw_payload(
                run, sequence=1, data=b"identical-bytes", content_type="application/json"
            )

        self.assertEqual(first.id, second.id)
        self.assertEqual(RawPayloadRecord.objects.filter(run=run, sequence=1).count(), 1)
        # the backend was asked to store the same content-addressed key
        # both times -- it's the backend's job (checked separately, in
        # test_s3_backend.py) to make the second call a no-op.
        first_key, second_key = (call.args[0] for call in fake_store.put.call_args_list)
        self.assertEqual(first_key, second_key)

    def test_conflicting_retry_preserves_both_versions(self):
        run = self._make_run()
        fake_store = MagicMock()
        fake_store.put.return_value = "s3://bucket/key"

        with patch("apps.rawstore.service.get_raw_payload_store", return_value=fake_store):
            first = store_raw_payload(
                run, sequence=1, data=b"first-attempt", content_type="application/json"
            )
            second = store_raw_payload(
                run, sequence=1, data=b"different-attempt", content_type="application/json"
            )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(RawPayloadRecord.objects.filter(run=run, sequence=1).count(), 2)
        self.assertEqual(first.checksum_sha256, hashlib.sha256(b"first-attempt").hexdigest())
        self.assertEqual(second.checksum_sha256, hashlib.sha256(b"different-attempt").hexdigest())
        # both point at distinct, content-addressed keys -- neither
        # storage call could have overwritten the other's object.
        first_key, second_key = (call.args[0] for call in fake_store.put.call_args_list)
        self.assertNotEqual(first_key, second_key)

    def test_storage_key_is_content_addressed(self):
        run = self._make_run()
        fake_store = MagicMock()
        fake_store.put.return_value = "s3://bucket/key"

        with patch("apps.rawstore.service.get_raw_payload_store", return_value=fake_store):
            store_raw_payload(run, sequence=3, data=b"x", content_type="application/json")

        key = fake_store.put.call_args[0][0]
        checksum = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(key, f"{run.pipeline_id}/{run.id}/000003-{checksum}.raw")

    def test_loaded_successfully_defaults_to_false(self):
        run = self._make_run()
        fake_store = MagicMock()
        fake_store.put.return_value = "s3://bucket/key"

        with patch("apps.rawstore.service.get_raw_payload_store", return_value=fake_store):
            record = store_raw_payload(run, sequence=1, data=b"x", content_type="application/json")

        self.assertFalse(record.loaded_successfully)

    def test_persists_extended_extract_metadata(self):
        run = self._make_run()
        fake_store = MagicMock()
        fake_store.put.return_value = "s3://bucket/key"

        with patch("apps.rawstore.service.get_raw_payload_store", return_value=fake_store):
            record = store_raw_payload(
                run,
                sequence=1,
                data=b"{}",
                content_type="application/json",
                cursor_used="1",
                next_cursor="2",
                item_count=5,
                source_path="/v1/policies",
                http_status=200,
            )

        self.assertEqual(record.cursor_used, "1")
        self.assertEqual(record.next_cursor, "2")
        self.assertEqual(record.item_count, 5)
        self.assertEqual(record.source_path, "/v1/policies")
        self.assertEqual(record.http_status, 200)
