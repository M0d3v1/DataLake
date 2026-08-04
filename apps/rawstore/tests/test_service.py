import hashlib
from unittest.mock import MagicMock, patch

from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline
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
