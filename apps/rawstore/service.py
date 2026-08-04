import hashlib

from apps.execution.models import PipelineRun
from apps.rawstore.base import get_raw_payload_store
from apps.rawstore.models import RawPayloadRecord


def store_raw_payload(
    run: PipelineRun, sequence: int, data: bytes, content_type: str
) -> RawPayloadRecord:
    """Persist one extract page/batch as an immutable raw payload and
    record its metadata. `sequence` is the page/batch order within the
    run (used both for a stable storage key and for replay ordering)."""
    checksum = hashlib.sha256(data).hexdigest()
    key = f"{run.pipeline_id}/{run.id}/{sequence:06d}.raw"
    uri = get_raw_payload_store().put(key, data, content_type=content_type)
    return RawPayloadRecord.objects.create(
        run=run,
        storage_uri=uri,
        content_type=content_type,
        size_bytes=len(data),
        checksum_sha256=checksum,
        sequence=sequence,
    )
