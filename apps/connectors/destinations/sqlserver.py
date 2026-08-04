"""Minimal SQL Server destination connector.

Reaches the destination exclusively through SQLAlchemy Core over pyodbc
-- never through Django's ORM/`DATABASES`, and never with more than the
credentials handed to it for this one operation (see
docs/decisions/0004-internal-external-db-separation.md). `load()` here is
a plain batched INSERT into an already-existing table; guided table/index
creation and upsert semantics are Milestone 2 work.
"""

from typing import Any

import sqlalchemy as sa

from apps.connectors.base import DestinationCapabilities, DestinationConnector
from apps.connectors.registry import register_destination
from apps.core.exceptions import ConnectionTestFailed, LoadFailed, UnsupportedCapability


@register_destination
class SqlServerDestinationConnector(DestinationConnector):
    """Config:
      - host (str), port (int, default 1433), database (str)
      - table_name (str): destination table `load()` writes into.
      - driver (str, default "ODBC Driver 18 for SQL Server")
      - extra_odbc_params (dict, optional): e.g. {"Encrypt": "yes"}

    `credential`:
      - username (str), password (secret)
    """

    type_key = "sqlserver"
    capabilities = DestinationCapabilities(
        supports_upsert=False, supports_guided_schema_creation=False
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
        return sa.create_engine(self._build_url(credential), pool_pre_ping=True)

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
                f"{self.type_key} destination connector does not support mode={mode!r} yet "
                "(upsert is planned for Milestone 2)"
            )
        if not records:
            return 0

        engine = self._engine(credential)
        try:
            metadata = sa.MetaData()
            table = sa.Table(self.config["table_name"], metadata, autoload_with=engine)
            with engine.begin() as conn:
                conn.execute(sa.insert(table), records)
        except sa.exc.SQLAlchemyError as exc:
            raise LoadFailed(str(exc)) from exc
        finally:
            engine.dispose()
        return len(records)
