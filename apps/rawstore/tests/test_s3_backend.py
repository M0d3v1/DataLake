import hashlib
from unittest.mock import MagicMock, patch

import botocore.exceptions
from django_tenants.test.cases import TenantTestCase

from apps.core.exceptions import (
    ConfigurationError,
    RawPayloadAccessDenied,
    RawPayloadIntegrityError,
)
from apps.rawstore.backends.s3 import S3RawPayloadStore


def _client_error(
    code: str, operation: str, status: int | None = None
) -> botocore.exceptions.ClientError:
    response = {"Error": {"Code": code, "Message": code}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return botocore.exceptions.ClientError(response, operation)


def _not_found_error(operation: str) -> botocore.exceptions.ClientError:
    return _client_error("404", operation)


def _no_such_bucket_error(operation: str) -> botocore.exceptions.ClientError:
    return _client_error("NoSuchBucket", operation)


def _precondition_failed_error(operation: str) -> botocore.exceptions.ClientError:
    return _client_error("PreconditionFailed", operation, status=412)


class S3RawPayloadStoreTests(TenantTestCase):
    def _store_with_mock_client(self):
        with patch("apps.rawstore.backends.s3.boto3.client") as mock_boto_client:
            store = S3RawPayloadStore()
        return store, mock_boto_client.return_value

    def test_init_does_not_create_the_bucket(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.list_buckets.assert_not_called()
        mock_client.create_bucket.assert_not_called()

    def test_put_prefixes_the_key_with_the_current_tenant_uuid_not_schema_name(self):
        store, mock_client = self._store_with_mock_client()

        uri = store.put("pipeline/run/000001-abc.raw", b"data")

        called_key = mock_client.put_object.call_args.kwargs["Key"]
        expected_prefix = str(self.tenant.tenant_uuid)
        self.assertTrue(called_key.startswith(f"{expected_prefix}/"))
        self.assertIn(called_key, uri)
        self.assertIn(expected_prefix, uri)
        # the schema name (a human-chosen, non-stable string) is NOT what
        # scopes the key -- this is the exact bug item 5 fixes.
        self.assertNotIn(self.tenant.schema_name, uri)

    def test_put_attempts_an_atomic_conditional_write_first(self):
        store, mock_client = self._store_with_mock_client()

        store.put("pipeline/run/000001-abc.raw", b"data", content_type="application/json")

        mock_client.put_object.assert_called_once()
        kwargs = mock_client.put_object.call_args.kwargs
        self.assertEqual(kwargs["IfNoneMatch"], "*")
        self.assertEqual(kwargs["ContentType"], "application/json")
        # the atomic path succeeds outright -- no separate existence
        # check, and no verification HEAD (nothing to verify against).
        mock_client.head_object.assert_not_called()

    def test_put_treats_precondition_failed_as_a_safe_no_op_after_verifying_size(self):
        store, mock_client = self._store_with_mock_client()
        data = b"data"
        mock_client.put_object.side_effect = _precondition_failed_error("PutObject")
        mock_client.head_object.return_value = {"ContentLength": len(data)}

        store.put("pipeline/run/000001-abc.raw", data)

        mock_client.head_object.assert_called_once()

    def test_put_raises_integrity_error_when_existing_object_size_differs(self):
        # Same content-addressed key but a different size on the object
        # already stored under it -- should be unreachable in practice
        # (the key embeds a checksum of the exact bytes), so this must be
        # surfaced loudly, not silently treated as a reuse.
        store, mock_client = self._store_with_mock_client()
        mock_client.put_object.side_effect = _precondition_failed_error("PutObject")
        mock_client.head_object.return_value = {"ContentLength": 999}

        with self.assertRaises(RawPayloadIntegrityError):
            store.put("pipeline/run/000001-abc.raw", b"data")

    def test_put_falls_back_to_check_then_write_when_conditional_writes_unsupported(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.put_object.side_effect = [
            _client_error("NotImplemented", "PutObject", status=501),
            None,  # the fallback's own put_object call succeeds
        ]
        mock_client.head_object.side_effect = _not_found_error("HeadObject")

        store.put("pipeline/run/000001-abc.raw", b"data")

        self.assertEqual(mock_client.put_object.call_count, 2)
        mock_client.head_object.assert_called_once()

    def test_put_fallback_skips_the_write_when_the_object_already_exists(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.put_object.side_effect = _client_error(
            "NotImplemented", "PutObject", status=501
        )
        mock_client.head_object.return_value = {}  # exists, no error

        store.put("pipeline/run/000001-abc.raw", b"data")

        mock_client.put_object.assert_called_once()  # only the failed conditional attempt

    def test_put_raises_configuration_error_when_bucket_missing(self):
        store, mock_client = self._store_with_mock_client()
        mock_client.put_object.side_effect = _no_such_bucket_error("PutObject")

        with self.assertRaises(ConfigurationError):
            store.put("pipeline/run/000001-abc.raw", b"data")

    def test_get_reads_back_a_previously_written_object(self):
        store, mock_client = self._store_with_mock_client()
        uri = store.put("pipeline/run/000001-abc.raw", b"data")

        body = MagicMock()
        body.read.return_value = b"data"
        mock_client.get_object.return_value = {"Body": body}

        result = store.get(uri)

        self.assertEqual(result, b"data")
        self.assertEqual(
            mock_client.get_object.call_args.kwargs["Key"],
            f"{self.tenant.tenant_uuid}/pipeline/run/000001-abc.raw",
        )

    def test_get_verifies_checksum_embedded_in_a_content_addressed_key(self):
        store, mock_client = self._store_with_mock_client()
        data = b'{"policy_id": "P-1"}'
        checksum = hashlib.sha256(data).hexdigest()
        uri = store.put(f"pipeline/run/000001-{checksum}.raw", data)

        body = MagicMock()
        body.read.return_value = data
        mock_client.get_object.return_value = {"Body": body}

        result = store.get(uri)  # must not raise -- checksum matches

        self.assertEqual(result, data)

    def test_get_raises_integrity_error_on_checksum_mismatch(self):
        store, mock_client = self._store_with_mock_client()
        checksum = hashlib.sha256(b"original-bytes").hexdigest()
        uri = store.put(f"pipeline/run/000001-{checksum}.raw", b"original-bytes")

        body = MagicMock()
        body.read.return_value = b"corrupted-or-tampered-bytes"
        mock_client.get_object.return_value = {"Body": body}

        with self.assertRaises(RawPayloadIntegrityError):
            store.get(uri)

    def test_get_rejects_malformed_uri(self):
        store, _mock_client = self._store_with_mock_client()
        with self.assertRaises(RawPayloadAccessDenied):
            store.get("not-a-uri")

    def test_get_rejects_a_different_bucket(self):
        store, _mock_client = self._store_with_mock_client()
        with self.assertRaises(RawPayloadAccessDenied):
            store.get(f"s3://some-other-bucket/{self.tenant.tenant_uuid}/pipeline/run/000001.raw")

    def test_get_rejects_a_cross_tenant_key(self):
        store, _mock_client = self._store_with_mock_client()
        bucket = store._bucket
        with self.assertRaises(RawPayloadAccessDenied):
            store.get(f"s3://{bucket}/some-other-tenant-uuid/pipeline/run/000001.raw")

    def test_ensure_bucket_is_never_called_by_put_or_get(self):
        store, mock_client = self._store_with_mock_client()

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

    def test_tenant_uuid_is_cached_across_calls_within_the_same_schema(self):
        store, mock_client = self._store_with_mock_client()

        store.put("pipeline/run/000001-abc.raw", b"data")
        store.put("pipeline/run/000002-def.raw", b"more-data")

        # apps.orgs.models.Organization is queried at most once per
        # schema, not once per put()/get() call.
        first_key = mock_client.put_object.call_args_list[0].kwargs["Key"]
        second_key = mock_client.put_object.call_args_list[1].kwargs["Key"]
        self.assertTrue(first_key.startswith(f"{self.tenant.tenant_uuid}/"))
        self.assertTrue(second_key.startswith(f"{self.tenant.tenant_uuid}/"))
