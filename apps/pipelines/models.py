import uuid

from django.conf import settings
from django.db import models

from apps.connections.models import Connection
from apps.core.models import TimeStampedModel


class Pipeline(TimeStampedModel):
    """A configured ingestion pipeline: one source, one destination, how
    records map between them, and (eventually) a schedule.

    `schedule_cron` is stored now but not yet acted on by a scheduler --
    Celery-beat-driven scheduling is Milestone 3. For Milestone 1/2,
    pipelines only run when manually triggered.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    source_connection = models.ForeignKey(
        Connection,
        on_delete=models.PROTECT,
        related_name="pipelines_as_source",
        limit_choices_to={"kind": Connection.Kind.SOURCE},
    )
    destination_connection = models.ForeignKey(
        Connection,
        on_delete=models.PROTECT,
        related_name="pipelines_as_destination",
        limit_choices_to={"kind": Connection.Kind.DESTINATION},
    )
    # Passed to the source connector's fetch() as `config` overrides
    # (e.g. a specific path/query) on top of the Connection's own config.
    extraction_config = models.JSONField(default=dict, blank=True)
    # destination_column -> source_field, mirrors
    # apps.connectors.base.DestinationMapping. Applied by
    # apps.pipelines.mapping before records reach the destination connector.
    destination_mapping = models.JSONField(default=dict, blank=True)
    # See apps.pipelines.mapping.map_record: strict raises on a missing
    # source field instead of substituting null.
    strict_mapping = models.BooleanField(default=False)
    schedule_cron = models.CharField(max_length=100, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    def __str__(self) -> str:
        return self.name
