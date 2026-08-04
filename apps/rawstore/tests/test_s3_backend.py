from unittest.mock import MagicMock, patch

import botocore.exceptions
from django_tenants.test.cases import TenantTestCase

from apps.core.exceptions import ConfigurationError, RawPayloadAccessDenied
from apps.rawstore.backends.s3 import S3RawPayloadStore


def _not_found_error(operation: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, operation
    )


def _no_such_bucket_error(operation: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "no bucket"}}, operation
    )


class S3RawPayloadStoreTests(TenantTestCase):
    def _store_with_mock_client(self):
        with patch("apps.rawstore.backends.s3.boto3.client") as mock_boto_client:
            store = S3RawPayloadStore()
        return store, mock_boto_client.return_value

    def test_init_does_not_create_the_bucket(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.list_buckets.assert_not_called()
        mock_client.create_bucket.assert_not_called()

    def test_put_prefixes_the_key_with_the_current_tenant_schema(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.head_object.side_effect = _not_found_error("HeadObject")

        uri = store.put("pipeline/run/000001-abc.raw", b"data")

        called_key = mock_client.put_object.call_args.kwargs["Key"]
        self.assertTrue(called_key.startswith(f"{self.tenant.schema_name}/"))
        self.assertIn(called_key, uri)  # the returned URI embeds that same tenant-prefixed key
        self.assertIn(self.tenant.schema_name, uri)

    def test_put_skips_the_write_when_the_object_already_exists(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.head_object.return_value = {}  # exists, no error

        store.put("pipeline/run/000001-abc.raw", b"data")

        mock_client.put_object.assert_not_called()

    def test_put_writes_when_the_object_is_absent(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.head_object.side_effect = _not_found_error("HeadObject")

        store.put("pipeline/run/000001-abc.raw", b"data", content_type="application/json")

        mock_client.put_object.assert_called_once()
        self.assertEqual(mock_client.put_object.call_args.kwargs["ContentType"], "application/json")

    def test_put_raises_configuration_error_when_bucket_missing(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.head_object.side_effect = _no_such_bucket_error("HeadObject")

        with self.assertRaises(ConfigurationError):
            store.put("pipeline/run/000001-abc.raw", b"data")

    def test_get_reads_back_a_previously_written_object(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.head_object.side_effect = _not_found_error("HeadObject")
        uri = store.put("pipeline/run/000001-abc.raw", b"data")

        body = MagicMock()
        body.read.return_value = b"data"
        mock_client.get_object.return_value = {"Body": body}

        result = store.get(uri)

        self.assertEqual(result, b"data")
        self.assertEqual(
            mock_client.get_object.call_args.kwargs["Key"],
            f"{self.tenant.schema_name}/pipeline/run/000001-abc.raw",
        )

    def test_get_rejects_malformed_uri(self):
        store, _mock_client = self._store_with_mock_client()
        with self.assertRaises(RawPayloadAccessDenied):
            store.get("not-a-uri")

    def test_get_rejects_a_different_bucket(self):
        store, _mock_client = self._store_with_mock_client()
        with self.assertRaises(RawPayloadAccessDenied):
            store.get(f"s3://some-other-bucket/{self.tenant.schema_name}/pipeline/run/000001.raw")

    def test_get_rejects_a_cross_tenant_key(self):
        store, _mock_client = self._store_with_mock_client()
        bucket = store._bucket
        with self.assertRaises(RawPayloadAccessDenied):
            store.get(f"s3://{bucket}/some-other-tenant/pipeline/run/000001.raw")

    def test_ensure_bucket_is_never_called_by_put_or_get(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.head_object.side_effect = _not_found_error("HeadObject")

        store.put("pipeline/run/000001-abc.raw", b"data")

        mock_client.create_bucket.assert_not_called()

    def test_ensure_bucket_creates_when_absent(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.list_buckets.return_value = {"Buckets": []}

        store.ensure_bucket()

        mock_client.create_bucket.assert_called_once_with(Bucket=store._bucket)

    def test_ensure_bucket_is_a_noop_when_already_present(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.list_buckets.return_value = {"Buckets": [{"Name": store._bucket}]}

        store.ensure_bucket()

        mock_client.create_bucket.assert_not_called()
