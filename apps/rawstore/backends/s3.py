import hashlib

import boto3
import botocore.exceptions
from django.conf import settings
from django.db import connection

from apps.core.exceptions import (
    ConfigurationError,
    RawPayloadAccessDenied,
    RawPayloadIntegrityError,
)
from apps.core.logging import get_logger
from apps.rawstore.base import RawPayloadStore

log = get_logger(__name__)

_URI_SCHEME = "s3://"

# Error codes/HTTP statuses that mean "this backend doesn't understand
# IfNoneMatch on PutObject", as opposed to "the object already exists"
# (PreconditionFailed / 412) or a real failure. Real AWS S3 and current
# MinIO both support conditional writes; this fallback exists for older
# or other S3-compatible backends that don't yet.
_CONDITIONAL_WRITE_UNSUPPORTED_CODES = {"NotImplemented", "XNotImplemented", "InvalidArgument"}
_CONDITIONAL_WRITE_UNSUPPORTED_STATUSES = {400, 501}


def _checksum_from_key(key: str) -> str | None:
    """Extract the sha256 checksum embedded in a content-addressed key of
    the form `.../{sequence:06d}-{checksum}.raw` (see
    `apps.rawstore.service.store_raw_payload`). Returns None for a key
    that doesn't match this shape rather than raising -- verification is
    a best-effort strengthening of a key format the backend doesn't own,
    not a hard requirement of every possible key."""
    filename = key.rsplit("/", 1)[-1]
    if not filename.endswith(".raw"):
        return None
    stem = filename[: -len(".raw")]
    _sequence, sep, checksum = stem.partition("-")
    if not sep or len(checksum) != 64:
        return None
    return checksum


class S3RawPayloadStore(RawPayloadStore):
    """S3-compatible object storage (MinIO for local/dev; any AWS S3
    compatible endpoint in production) for immutable raw payloads.

    Tenant isolation and non-overwrite are enforced here, not left to
    callers: `put()` prepends the current tenant's stable UUID (not
    `schema_name` -- a human-chosen, mutable-in-principle string, and not
    the tenant's small sequential integer `id`, which would make key
    prefixes an enumeration of every tenant on the deployment) to
    whatever logical key it's given, and only writes if that exact key
    doesn't already exist, preferring an atomic conditional write where
    the backend supports it (callers are expected to make the key
    content-addressed -- see `apps.rawstore.service.store_raw_payload` --
    so "already exists" means "byte-identical", not "stale"; that claim
    is verified, not assumed, before treating a pre-existing object as a
    safe reuse). `get()` refuses to read outside the configured bucket or
    outside the currently active tenant's prefix, and verifies the
    downloaded bytes' checksum against the one embedded in the key.
    Bucket creation is a separate, explicit operational step (see
    `ensure_bucket()` / `manage.py provision_raw_store`), never performed
    as a side effect of a normal read or write.

    Note on immutability's boundary: everything above is an
    *application-level* guarantee -- enforced by this code, for callers
    that go through it. It does not, by itself, prevent someone with
    direct storage-layer credentials (e.g. AWS console/API access to the
    bucket) from deleting or overwriting an object outside this code
    path. A true storage-level guarantee needs the backend's own
    immutability controls (e.g. S3 Object Lock in compliance mode) --
    see docs/decisions/0007-raw-payload-immutability.md.
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
        self._tenant_uuid_cache: dict[str, str] = {}

    def ensure_bucket(self) -> None:
        """Explicit, operator-triggered provisioning step -- see
        `manage.py provision_raw_store`. Never called from `put()`/`get()`."""
        existing = {b["Name"] for b in self._client.list_buckets().get("Buckets", [])}
        if self._bucket not in existing:
            self._client.create_bucket(Bucket=self._bucket)

    def _tenant_uuid(self) -> str:
        schema_name = connection.schema_name
        cached = self._tenant_uuid_cache.get(schema_name)
        if cached is not None:
            return cached

        from apps.orgs.models import Organization

        try:
            tenant_uuid = str(Organization.objects.get(schema_name=schema_name).tenant_uuid)
        except Organization.DoesNotExist as exc:
            raise ConfigurationError(
                f"no Organization found for schema {schema_name!r}; raw payload storage "
                "requires an active tenant schema"
            ) from exc
        self._tenant_uuid_cache[schema_name] = tenant_uuid
        return tenant_uuid

    def _tenant_key(self, key: str) -> str:
        return f"{self._tenant_uuid()}/{key}"

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
        written = self._put_create_only(tenant_key, data, content_type)
        if not written:
            self._verify_existing_object(tenant_key, data)
        return f"{_URI_SCHEME}{self._bucket}/{tenant_key}"

    def _put_create_only(self, tenant_key: str, data: bytes, content_type: str) -> bool:
        """Write `data` to `tenant_key` only if absent. Returns True if
        this call actually wrote the object, False if it already
        existed. Prefers an atomic conditional write (`IfNoneMatch="*"`)
        so two concurrent retries of the same page can never race into an
        overwrite; falls back to a non-atomic check-then-write only if
        the backend signals it doesn't support conditional PutObject."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=tenant_key,
                Body=data,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "PreconditionFailed" or status == 412:
                return False
            if code == "NoSuchBucket":
                raise ConfigurationError(
                    f"raw payload bucket {self._bucket!r} does not exist; "
                    "run `manage.py provision_raw_store` before running pipelines"
                ) from exc
            if code in _CONDITIONAL_WRITE_UNSUPPORTED_CODES or status in (
                _CONDITIONAL_WRITE_UNSUPPORTED_STATUSES
            ):
                log.warning(
                    "rawstore.s3.conditional_write_unsupported", key=tenant_key, code=code
                )
                return self._put_check_then_write(tenant_key, data, content_type)
            raise

    def _put_check_then_write(self, tenant_key: str, data: bytes, content_type: str) -> bool:
        if self._exists(tenant_key):
            return False
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
        return True

    def _verify_existing_object(self, tenant_key: str, data: bytes) -> None:
        """Before trusting a pre-existing object at a content-addressed
        key as a safe reuse, confirm it actually matches: same key is
        only supposed to mean "same bytes" (the key embeds a checksum of
        `data`), so a size mismatch here means either a SHA-256 collision
        or storage-layer corruption/tampering -- either way, not
        something to silently treat as identical."""
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=tenant_key)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise RawPayloadIntegrityError(
                    f"raw payload object at {tenant_key!r} vanished between the create-only "
                    "write and post-write verification"
                ) from exc
            raise
        existing_size = head.get("ContentLength")
        if existing_size is not None and existing_size != len(data):
            raise RawPayloadIntegrityError(
                f"raw payload object at {tenant_key!r} already exists with a different size "
                f"({existing_size} bytes stored vs {len(data)} bytes being written) despite "
                "an identical content-addressed key -- refusing to treat this as a safe reuse"
            )

    def get(self, uri: str) -> bytes:
        bucket, key = self._parse_and_authorize(uri)
        response = self._client.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()

        expected_checksum = _checksum_from_key(key)
        if expected_checksum is not None:
            actual_checksum = hashlib.sha256(data).hexdigest()
            if actual_checksum != expected_checksum:
                raise RawPayloadIntegrityError(
                    f"raw payload at {uri!r} failed checksum verification on read "
                    f"(expected {expected_checksum}, got {actual_checksum}) -- this indicates "
                    "storage-layer corruption or an object modified outside this platform, "
                    "not an application-level mutation (see docs/decisions/0007-raw-payload-"
                    "immutability.md for the application-level vs. storage-level boundary)"
                )
        return data

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
        if tenant_prefix != self._tenant_uuid():
            raise RawPayloadAccessDenied(
                "refusing to read a raw payload belonging to a different tenant"
            )
        return bucket, key
