import uuid

from django.db import models

from apps.core.models import TimeStampedModel
from apps.execution.models import PipelineRun


class RawPayloadRecord(TimeStampedModel):
    """Metadata for one immutable raw response captured during a
    PipelineRun's extract step. The payload bytes live in object storage
    (`apps.rawstore.base.RawPayloadStore`); this row is what makes them
    discoverable, checksummed, and auditable."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="raw_payloads")
    storage_uri = models.CharField(max_length=1024)
    content_type = models.CharField(max_length=100, default="application/json")
    size_bytes = models.PositiveBigIntegerField()
    checksum_sha256 = models.CharField(max_length=64)
    sequence = models.PositiveIntegerField(help_text="Order within the run, e.g. page number")

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "sequence"], name="unique_raw_payload_sequence_per_run"
            )
        ]

    def __str__(self) -> str:
        return f"RawPayloadRecord(run={self.run_id}, seq={self.sequence})"
