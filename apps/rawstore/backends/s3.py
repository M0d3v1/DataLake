import boto3
from django.conf import settings

from apps.rawstore.base import RawPayloadStore


class S3RawPayloadStore(RawPayloadStore):
    """S3-compatible object storage (MinIO for local/dev; any AWS S3
    compatible endpoint in production) for immutable raw payloads."""

    def __init__(self) -> None:
        self._bucket = settings.RAW_STORE_BUCKET
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.RAW_STORE_ENDPOINT_URL,
            aws_access_key_id=settings.RAW_STORE_ACCESS_KEY,
            aws_secret_access_key=settings.RAW_STORE_SECRET_KEY,
            use_ssl=settings.RAW_STORE_USE_SSL,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        existing = {b["Name"] for b in self._client.list_buckets().get("Buckets", [])}
        if self._bucket not in existing:
            self._client.create_bucket(Bucket=self._bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self._bucket}/{key}"

    def get(self, uri: str) -> bytes:
        prefix = "s3://"
        if not uri.startswith(prefix):
            raise ValueError(f"Not an s3:// URI: {uri!r}")
        bucket, _, key = uri[len(prefix) :].partition("/")
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
