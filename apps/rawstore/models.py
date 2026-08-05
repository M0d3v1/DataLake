import uuid

from django.db import models

from apps.core.models import TimeStampedModel
from apps.execution.models import PipelineRun


class RawPayloadRecord(TimeStampedModel):
    """Metadata for one immutable raw response captured during a
    PipelineRun's extract step. The payload bytes live in object storage
    (`apps.rawstore.base.RawPayloadStore`), under a tenant-scoped,
    checksum-addressed key that the store backend never overwrites; this
    row is what makes them discoverable, checksummed, and auditable.

    A given `(run, sequence)` can have more than one row: if a retry
    fetches the same logical page but the bytes differ from the first
    attempt (the source changed between attempts), that's a *different*
    checksum, a *different* object, and a *second* row here -- both
    versions are preserved rather than one silently overwriting the
    other. See docs/decisions/0007-raw-payload-immutability.md.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="raw_payloads")
    storage_uri = models.CharField(max_length=1024)
    content_type = models.CharField(max_length=100, default="application/json")
    size_bytes = models.PositiveBigIntegerField()
    checksum_sha256 = models.CharField(max_length=64)
    sequence = models.PositiveIntegerField(help_text="Order within the run, e.g. page number")

    # Set by the orchestrator once (and only if) this specific version's
    # records were successfully mapped and loaded at the destination --
    # distinguishes "we stored this" from "this is the version we acted
    # on", which matters once more than one version can exist per page.
    loaded_successfully = models.BooleanField(default=False)

    # --- Milestone 2: extract-context metadata, all optional so a
    # connector that doesn't report them (or a raw store used outside the
    # REST connector) still works. ------------------------------------
    cursor_used = models.CharField(max_length=255, null=True, blank=True)
    next_cursor = models.CharField(max_length=255, null=True, blank=True)
    item_count = models.PositiveIntegerField(default=0)
    source_path = models.CharField(max_length=500, blank=True, default="")
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "sequence", "checksum_sha256"],
                name="unique_raw_payload_version_per_run",
            )
        ]

    def __str__(self) -> str:
        return f"RawPayloadRecord(run={self.run_id}, seq={self.sequence})"
