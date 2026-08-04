import boto3
import botocore.exceptions
from django.conf import settings
from django.db import connection

from apps.core.exceptions import ConfigurationError, RawPayloadAccessDenied
from apps.rawstore.base import RawPayloadStore

_URI_SCHEME = "s3://"


class S3RawPayloadStore(RawPayloadStore):
    """S3-compatible object storage (MinIO for local/dev; any AWS S3
    compatible endpoint in production) for immutable raw payloads.

    Tenant isolation and non-overwrite are enforced here, not left to
    callers: `put()` prepends the *current* tenant schema to whatever
    logical key it's given, and only writes if that exact key doesn't
    already exist (callers are expected to make the key
    content-addressed -- see `apps.rawstore.service.store_raw_payload` --
    so "already exists" means "byte-identical", not "stale"). `get()`
    refuses to read outside the configured bucket or outside the
    currently active tenant's prefix. Bucket creation is a separate,
    explicit operational step (see `ensure_bucket()` /
    `manage.py provision_raw_store`), never performed as a side effect of
    a normal read or write.
    """

    def __init__(self) -> None:
        self._bucket = settings.RAW_STORE_BUCKET
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.RAW_STORE_ENDPOINT_URL,
            aws_access_key_id=settings.RAW_STORE_ACCESS_KEY,
            aws_secret_access_key=settings.RAW_STORE_SECRET_KEY,
            use_ssl=settings.RAW_STORE_USE_SSL,
        )

    def ensure_bucket(self) -> None:
        """Explicit, operator-triggered provisioning step -- see
        `manage.py provision_raw_store`. Never called from `put()`/`get()`."""
        existing = {b["Name"] for b in self._client.list_buckets().get("Buckets", [])}
        if self._bucket not in existing:
            self._client.create_bucket(Bucket=self._bucket)

    def _tenant_key(self, key: str) -> str:
        return f"{connection.schema_name}/{key}"

    def _exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            if code in ("NoSuchBucket",):
                raise ConfigurationError(
                    f"raw payload bucket {self._bucket!r} does not exist; "
                    "run `manage.py provision_raw_store` before running pipelines"
                ) from exc
            raise

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        tenant_key = self._tenant_key(key)
        if not self._exists(tenant_key):
            try:
                self._client.put_object(
                    Bucket=self._bucket, Key=tenant_key, Body=data, ContentType=content_type
                )
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "NoSuchBucket":
                    raise ConfigurationError(
                        f"raw payload bucket {self._bucket!r} does not exist; "
                        "run `manage.py provision_raw_store` before running pipelines"
                    ) from exc
                raise
        return f"{_URI_SCHEME}{self._bucket}/{tenant_key}"

    def get(self, uri: str) -> bytes:
        bucket, key = self._parse_and_authorize(uri)
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    def _parse_and_authorize(self, uri: str) -> tuple[str, str]:
        if not uri.startswith(_URI_SCHEME):
            raise RawPayloadAccessDenied(f"not a valid raw payload URI: {uri!r}")
        bucket, _, key = uri[len(_URI_SCHEME) :].partition("/")
        if not bucket or not key:
            raise RawPayloadAccessDenied(f"not a valid raw payload URI: {uri!r}")
        if bucket != self._bucket:
            raise RawPayloadAccessDenied(
                f"refusing to read from bucket {bucket!r}; only {self._bucket!r} is configured"
            )
        tenant_prefix = key.split("/", 1)[0]
        if tenant_prefix != connection.schema_name:
            raise RawPayloadAccessDenied(
                "refusing to read a raw payload belonging to a different tenant"
            )
        return bucket, key
