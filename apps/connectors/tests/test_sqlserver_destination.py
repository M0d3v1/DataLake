from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from apps.connectors.destinations.sqlserver import SqlServerDestinationConnector
from apps.core.exceptions import ConnectionTestFailed, UnsupportedCapability


def _connector(**config_overrides):
    config = {
        "host": "db.example-insurer.test",
        "database": "warehouse",
        "table_name": "policies",
    }
    config.update(config_overrides)
    return SqlServerDestinationConnector(config)


def test_build_url_uses_config_and_credential():
    url = _connector()._build_url({"username": "svc", "password": "pw"})
    assert url.drivername == "mssql+pyodbc"
    assert url.username == "svc"
    assert url.password == "pw"
    assert url.host == "db.example-insurer.test"
    assert url.database == "warehouse"
    assert url.query["driver"] == "ODBC Driver 18 for SQL Server"


def test_test_connection_success():
    connector = _connector()
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = mock_conn

    with patch(
        "apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine
    ):
        connector.test_connection({"username": "svc", "password": "pw"})

    mock_conn.execute.assert_called_once()
    mock_engine.dispose.assert_called_once()


def test_test_connection_failure_raises_connection_test_failed():
    connector = _connector()
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = sa.exc.OperationalError("stmt", {}, Exception("boom"))

    with patch(
        "apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine
    ):
        with pytest.raises(ConnectionTestFailed):
            connector.test_connection({"username": "svc", "password": "pw"})

    mock_engine.dispose.assert_called_once()


def test_load_rejects_unsupported_mode():
    connector = _connector()
    with pytest.raises(UnsupportedCapability):
        connector.load({"username": "svc", "password": "pw"}, [{"id": 1}], mode="upsert")


def test_load_with_no_records_is_a_noop():
    connector = _connector()
    assert connector.load({"username": "svc", "password": "pw"}, [], mode="append") == 0
