"""Connector interfaces.

A "connector" is the only place platform code talks to an external
system. Two flavors:

  - SourceConnector: pulls records (+ the raw payload they came from)
    from an external API or database.
  - DestinationConnector: writes records into an external database.

Connectors declare their own capabilities so the orchestration layer
(apps.execution) can adapt instead of hard-coding per-connector behavior
-- e.g. falling back to a full refresh when a source doesn't support
incremental sync, rather than assuming every connector can.

`config` (passed at construction) holds non-secret settings (host, path,
pagination options, table name, ...). `credential` (passed per call) is
the *decrypted* secret payload, resolved by the caller via
`apps.secrets.get_secret_store()` immediately before use and never
persisted on the connector instance.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from apps.core.exceptions import UnsupportedCapability


@dataclass(frozen=True)
class SourceCapabilities:
    supports_incremental: bool = False
    supports_pagination: bool = True


@dataclass(frozen=True)
class FetchResult:
    """One page/batch from a source connector."""

    records: list[dict[str, Any]]
    next_cursor: str | None
    raw_payload: bytes
    raw_content_type: str = "application/json"
    # Optional, connector-specific metadata persisted onto the
    # RawPayloadRecord for this page (apps.rawstore) -- all have safe
    # defaults so existing connectors/tests that don't set them keep working.
    cursor_used: str | None = None
    source_path: str | None = None
    http_status: int | None = None


class SourceConnector(ABC):
    type_key: ClassVar[str]
    capabilities: ClassVar[SourceCapabilities] = SourceCapabilities()

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def test_connection(self, credential: dict[str, Any]) -> None:
        """Raise `apps.core.exceptions.ConnectionTestFailed` on failure."""

    @abstractmethod
    def fetch(self, credential: dict[str, Any], cursor: str | None = None) -> Iterator[FetchResult]:
        """Yield one `FetchResult` per page/batch, starting from `cursor`
        (None means "from the beginning"). Callers persist the last
        `next_cursor` they saw to support resuming/incremental sync,
        subject to `capabilities.supports_incremental`."""


@dataclass(frozen=True)
class DestinationMapping:
    """Where and how fetched records are written at the destination."""

    table_name: str
    # destination_column -> source_field
    columns: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DestinationCapabilities:
    supports_upsert: bool = False
    supports_guided_schema_creation: bool = False
    # Whether bulk_load() below is implemented -- required for a
    # pipeline to run in Spark mode (Pipeline.processing_mode == SPARK),
    # checked at job-submission time by
    # apps.sparktransform.services.validate_spark_transform_config. See
    # docs/decisions/0009-spark-backed-transform-mode.md.
    supports_bulk_load: bool = False


class DestinationConnector(ABC):
    type_key: ClassVar[str]
    capabilities: ClassVar[DestinationCapabilities] = DestinationCapabilities()

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def test_connection(self, credential: dict[str, Any]) -> None:
        """Raise `apps.core.exceptions.ConnectionTestFailed` on failure."""

    def ensure_schema(self, credential: dict[str, Any], mapping: DestinationMapping) -> None:
        """Create or verify the destination table matches `mapping`.

        Default implementation raises NotImplementedError; connectors
        that support guided schema creation should override this and set
        `capabilities.supports_guided_schema_creation = True`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support guided schema creation")

    @abstractmethod
    def load(
        self, credential: dict[str, Any], records: list[dict[str, Any]], *, mode: str = "append"
    ) -> int:
        """Write `records` to the destination, return the count written."""

    def bulk_load(
        self, credential: dict[str, Any], records: list[dict[str, Any]], *, mode: str = "append"
    ) -> int:
        """Write `records` (already destination-shaped, e.g. a Spark
        transform job's output) using a faster bulk mechanism than
        `load()`'s per-batch inserts -- same interface, same audit hooks,
        a different implementation underneath (for SQL Server: a staging
        table + one bulk copy + one INSERT...SELECT, all in a single
        transaction, rather than many small ones). Required for a
        pipeline to run in Spark mode -- see
        `capabilities.supports_bulk_load` and
        docs/decisions/0009-spark-backed-transform-mode.md.

        Default implementation raises `UnsupportedCapability`; connectors
        that support bulk loading must override this and set
        `capabilities.supports_bulk_load = True`.

        Note on scope: like `load()`, this still stages `records` as an
        in-memory Python list -- it is not (yet) a true out-of-core bulk
        transfer where the destination reads directly from object
        storage. That's real future work, not pretended-away here.
        """
        raise UnsupportedCapability(f"{type(self).__name__} does not support bulk load")
