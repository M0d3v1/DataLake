import hashlib

from apps.execution.models import PipelineRun
from apps.rawstore.base import get_raw_payload_store
from apps.rawstore.models import RawPayloadRecord


def store_raw_payload(
    run: PipelineRun,
    *,
    sequence: int,
    data: bytes,
    content_type: str,
    cursor_used: str | None = None,
    next_cursor: str | None = None,
    item_count: int = 0,
    source_path: str = "",
    http_status: int | None = None,
) -> RawPayloadRecord:
    """Persist one extract page/batch as an immutable raw payload and
    record its metadata. `sequence` is the page/batch order within the
    run, used both for a deterministic storage key and as the natural
    unique key for this run's pages.

    Idempotent by design: the storage key is deterministic
    (`{pipeline}/{run}/{sequence}.raw`), so re-uploading on a retried page
    just overwrites the same object, and the metadata row is
    `update_or_create`d on `(run, sequence)` rather than always inserted
    -- a retried page updates its existing record instead of colliding
    with the unique constraint or creating a duplicate.
    """
    checksum = hashlib.sha256(data).hexdigest()
    key = f"{run.pipeline_id}/{run.id}/{sequence:06d}.raw"
    uri = get_raw_payload_store().put(key, data, content_type=content_type)
    record, _created = RawPayloadRecord.objects.update_or_create(
        run=run,
        sequence=sequence,
        defaults={
            "storage_uri": uri,
            "content_type": content_type,
            "size_bytes": len(data),
            "checksum_sha256": checksum,
            "cursor_used": cursor_used,
            "next_cursor": next_cursor,
            "item_count": item_count,
            "source_path": source_path,
            "http_status": http_status,
        },
    )
    return record
