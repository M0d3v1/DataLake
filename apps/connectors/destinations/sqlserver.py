"""SQL Server destination connector.

Reaches the destination exclusively through SQLAlchemy Core over pyodbc
-- never through Django's ORM/`DATABASES`, and never with more than the
credentials handed to it for this one operation (see
docs/decisions/0004-internal-external-db-separation.md). `load()` writes
already-mapped records (destination column names as keys) into an
existing table in bounded, single-transaction batches.

This is at-least-once, not exactly-once: see
docs/decisions/0005-execution-orchestration.md for the duplicate-risk
window this implies when a worker crashes between a destination commit
and the platform recording that success.
"""

import re
import uuid
from typing import Any

import sqlalchemy as sa

from apps.connectors.base import DestinationCapabilities, DestinationConnector
from apps.connectors.registry import register_destination
from apps.core.exceptions import (
    ConfigurationError,
    ConnectionTestFailed,
    LoadFailed,
    SchemaMismatchError,
    UnsupportedCapability,
)

DEFAULT_BATCH_SIZE = 500
DEFAULT_BULK_BATCH_SIZE = 2000
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _validate_sql_identifier(name: Any, *, label: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ConfigurationError(
            f"{label} {name!r} is not a valid SQL identifier "
            "(letters, digits, underscore; must not start with a digit; max 128 chars)"
        )
    return name


@register_destination
class SqlServerDestinationConnector(DestinationConnector):
    """Config:
      - host (str), port (int, default 1433), database (str)
      - schema_name (str, default "dbo")
      - table_name (str): destination table `load()` writes into.
      - driver (str, default "ODBC Driver 18 for SQL Server")
      - extra_odbc_params (dict, optional): e.g. {"Encrypt": "yes"}
      - batch_size (int, default 500): rows per bounded INSERT batch.

    `credential`:
      - username (str), password (secret)
    """

    type_key = "sqlserver"
    capabilities = DestinationCapabilities(
        supports_upsert=False, supports_guided_schema_creation=False, supports_bulk_load=True
    )

    def _build_url(self, credential: dict[str, Any]) -> sa.engine.URL:
        query = {"driver": self.config.get("driver", "ODBC Driver 18 for SQL Server")}
        query.update(self.config.get("extra_odbc_params", {}))
        return sa.engine.URL.create(
            "mssql+pyodbc",
            username=credential.get("username"),
            password=credential.get("password"),
            host=self.config["host"],
            port=self.config.get("port", 1433),
            database=self.config["database"],
            query=query,
        )

    def _engine(self, credential: dict[str, Any]) -> sa.engine.Engine:
        # hide_parameters: bound INSERT values (business data, not
        # secrets) must not end up verbatim in exception text -- see
        # apps.core.redaction and docs/decisions/0005-execution-orchestration.md.
        return sa.create_engine(
            self._build_url(credential),
            pool_pre_ping=True,
            hide_parameters=True,
            fast_executemany=True,
        )

    def test_connection(self, credential: dict[str, Any]) -> None:
        engine = self._engine(credential)
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
        except sa.exc.SQLAlchemyError as exc:
            raise ConnectionTestFailed(str(exc)) from exc
        finally:
            engine.dispose()

    def load(
        self, credential: dict[str, Any], records: list[dict[str, Any]], *, mode: str = "append"
    ) -> int:
        if mode != "append":
            raise UnsupportedCapability(
                f"{self.type_key} destination connector only supports mode='append' "
                f"(got {mode!r}); upsert/CDC are deliberately deferred, see the roadmap"
            )
        if not records:
            return 0

        schema_name = _validate_sql_identifier(
            self.config.get("schema_name", "dbo"), label="schema_name"
        )
        table_name = _validate_sql_identifier(self.config["table_name"], label="table_name")
        batch_size = self.config.get("batch_size", DEFAULT_BATCH_SIZE)
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ConfigurationError(
                "sqlserver destination: 'batch_size' must be a positive integer"
            )

        engine = self._engine(credential)
        try:
            table = self._reflect_table(engine, schema_name, table_name)
            self._validate_columns(table, records, schema_name, table_name)

            total_loaded = 0
            with engine.begin() as conn:
                for start in range(0, len(records), batch_size):
                    batch = records[start : start + batch_size]
                    conn.execute(sa.insert(table), batch)
                    total_loaded += len(batch)
            return total_loaded
        except sa.exc.OperationalError as exc:
            # Connectivity loss, lock-wait timeout, deadlock victim, etc.
            # -- SQLAlchemy/pyodbc surface these as OperationalError across
            # the board; a retry with a fresh connection can succeed.
            raise LoadFailed(
                f"database connectivity error during load: {exc}", retryable=True
            ) from exc
        except sa.exc.IntegrityError as exc:
            raise LoadFailed(
                f"integrity constraint violated during load: {exc}", retryable=False
            ) from exc
        except sa.exc.SQLAlchemyError as exc:
            raise LoadFailed(str(exc), retryable=False) from exc
        finally:
            engine.dispose()

    def bulk_load(
        self, credential: dict[str, Any], records: list[dict[str, Any]], *, mode: str = "append"
    ) -> int:
        """Same validation and outcome as `load()`, but writes through a
        temporary staging table (created from the destination table's own
        shape, dropped again in the same transaction) with a larger batch
        size, then moves everything into the destination with one
        `INSERT ... SELECT`, rather than many small single-table
        transactions. Real bulk-copy semantics (SQL Server reading
        directly from a file/blob) are future work -- see the docstring
        on `DestinationConnector.bulk_load`."""
        if mode != "append":
            raise UnsupportedCapability(
                f"{self.type_key} destination connector only supports mode='append' "
                f"(got {mode!r}); upsert/CDC are deliberately deferred, see the roadmap"
            )
        if not records:
            return 0

        schema_name = _validate_sql_identifier(
            self.config.get("schema_name", "dbo"), label="schema_name"
        )
        table_name = _validate_sql_identifier(self.config["table_name"], label="table_name")
        batch_size = self.config.get("bulk_batch_size", DEFAULT_BULK_BATCH_SIZE)
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ConfigurationError(
                "sqlserver destination: 'bulk_batch_size' must be a positive integer"
            )
        # Generated, not tenant-supplied -- still validated for defense
        # in depth, since it's about to be interpolated into DDL/DML text
        # that SQLAlchemy's query builder can't parameterize an
        # identifier into.
        staging_table_name = _validate_sql_identifier(
            f"__bulk_staging_{uuid.uuid4().hex[:12]}", label="staging_table_name"
        )

        engine = self._engine(credential)
        try:
            table = self._reflect_table(engine, schema_name, table_name)
            self._validate_columns(table, records, schema_name, table_name)

            qualified_table = f"[{schema_name}].[{table_name}]"
            qualified_staging = f"[{schema_name}].[{staging_table_name}]"
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        f"SELECT TOP 0 * INTO {qualified_staging} FROM {qualified_table}"
                    )
                )
                staging_table = sa.Table(
                    staging_table_name, sa.MetaData(schema=schema_name), autoload_with=conn
                )
                for start in range(0, len(records), batch_size):
                    batch = records[start : start + batch_size]
                    conn.execute(sa.insert(staging_table), batch)
                conn.execute(
                    sa.text(f"INSERT INTO {qualified_table} SELECT * FROM {qualified_staging}")
                )
                conn.execute(sa.text(f"DROP TABLE {qualified_staging}"))
            return len(records)
        except sa.exc.OperationalError as exc:
            raise LoadFailed(
                f"database connectivity error during bulk load: {exc}", retryable=True
            ) from exc
        except sa.exc.IntegrityError as exc:
            raise LoadFailed(
                f"integrity constraint violated during bulk load: {exc}", retryable=False
            ) from exc
        except sa.exc.SQLAlchemyError as exc:
            raise LoadFailed(str(exc), retryable=False) from exc
        finally:
            engine.dispose()

    def _reflect_table(
        self, engine: sa.engine.Engine, schema_name: str, table_name: str
    ) -> sa.Table:
        metadata = sa.MetaData(schema=schema_name)
        try:
            return sa.Table(table_name, metadata, autoload_with=engine, schema=schema_name)
        except sa.exc.NoSuchTableError as exc:
            raise SchemaMismatchError(
                f"destination table {schema_name}.{table_name} does not exist"
            ) from exc

    def _validate_columns(
        self, table: sa.Table, records: list[dict[str, Any]], schema_name: str, table_name: str
    ) -> None:
        known_columns = set(table.columns.keys())
        record_columns: set[str] = set()
        for record in records:
            record_columns.update(record.keys())
        unknown_columns = record_columns - known_columns
        if unknown_columns:
            raise SchemaMismatchError(
                f"destination table {schema_name}.{table_name} has no column(s) "
                f"{sorted(unknown_columns)}; known columns: {sorted(known_columns)}"
            )
