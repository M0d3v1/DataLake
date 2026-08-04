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
    run; the storage key also includes a checksum of `data`, so payload
    *identity* is content-addressed, not just position-addressed.

    Idempotent by construction, not by best-effort overwrite: a retried
    page with byte-identical content hashes to the same key, so the
    store backend's own non-overwrite check makes the write a no-op, and
    `get_or_create` here finds the existing `(run, sequence, checksum)`
    row rather than inserting a duplicate. A retried page with
    *different* bytes (the source changed between attempts) hashes to a
    different key and gets a genuinely new row -- both versions are kept
    side by side rather than one silently replacing the other. See
    docs/decisions/0007-raw-payload-immutability.md.

    The tenant boundary is enforced by the store backend itself (it
    prepends the active tenant's schema to whatever key it's given), not
    by this function -- see `apps.rawstore.base.RawPayloadStore`.
    """
    checksum = hashlib.sha256(data).hexdigest()
    key = f"{run.pipeline_id}/{run.id}/{sequence:06d}-{checksum}.raw"
    uri = get_raw_payload_store().put(key, data, content_type=content_type)
    record, _created = RawPayloadRecord.objects.get_or_create(
        run=run,
        sequence=sequence,
        checksum_sha256=checksum,
        defaults={
            "storage_uri": uri,
            "content_type": content_type,
            "size_bytes": len(data),
            "cursor_used": cursor_used,
            "next_cursor": next_cursor,
            "item_count": item_count,
            "source_path": source_path,
            "http_status": http_status,
        },
    )
    return record
