from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from apps.connectors.destinations.sqlserver import SqlServerDestinationConnector
from apps.core.exceptions import (
    ConfigurationError,
    ConnectionTestFailed,
    LoadFailed,
    SchemaMismatchError,
    UnsupportedCapability,
)

CREDENTIAL = {"username": "svc", "password": "pw"}


def _connector(**config_overrides):
    config = {
        "host": "db.example-insurer.test",
        "database": "warehouse",
        "table_name": "policies",
    }
    config.update(config_overrides)
    return SqlServerDestinationConnector(config)


def _fake_table(columns: set[str]) -> sa.Table:
    # A real (unbound) sa.Table so sa.insert(table) accepts it -- a bare
    # MagicMock fails SQLAlchemy's internal type coercion.
    metadata = sa.MetaData()
    return sa.Table("fake_table", metadata, *(sa.Column(name, sa.String) for name in columns))


# --- connection URL / test_connection (existing M1 coverage, kept) -------


def test_build_url_uses_config_and_credential():
    url = _connector()._build_url(CREDENTIAL)
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

    with patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine):
        connector.test_connection(CREDENTIAL)

    mock_conn.execute.assert_called_once()
    mock_engine.dispose.assert_called_once()


def test_test_connection_failure_raises_connection_test_failed():
    connector = _connector()
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = sa.exc.OperationalError("stmt", {}, Exception("boom"))

    with patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine):
        with pytest.raises(ConnectionTestFailed):
            connector.test_connection(CREDENTIAL)

    mock_engine.dispose.assert_called_once()


# --- load(): mode / empty records -----------------------------------------


def test_load_rejects_unsupported_mode():
    connector = _connector()
    with pytest.raises(UnsupportedCapability):
        connector.load(CREDENTIAL, [{"id": 1}], mode="upsert")


def test_load_with_no_records_is_a_noop():
    connector = _connector()
    assert connector.load(CREDENTIAL, [], mode="append") == 0


# --- SQL identifier validation ---------------------------------------------


@pytest.mark.parametrize("bad_table_name", ["policies; DROP TABLE x", "1policies", "", "a b"])
def test_load_rejects_invalid_table_name(bad_table_name):
    connector = _connector(table_name=bad_table_name)
    with pytest.raises(ConfigurationError):
        connector.load(CREDENTIAL, [{"id": 1}], mode="append")


def test_load_rejects_invalid_schema_name():
    connector = _connector(schema_name="dbo; DROP TABLE x")
    with pytest.raises(ConfigurationError):
        connector.load(CREDENTIAL, [{"id": 1}], mode="append")


def test_load_accepts_valid_identifiers_and_reaches_reflection():
    connector = _connector(table_name="valid_table_1", schema_name="dbo")
    mock_engine = MagicMock()
    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(
            SqlServerDestinationConnector, "_reflect_table", side_effect=SchemaMismatchError("stub")
        ),
    ):
        # Getting to _reflect_table (rather than failing identifier
        # validation first) proves the valid names passed validation.
        with pytest.raises(SchemaMismatchError):
            connector.load(CREDENTIAL, [{"id": 1}], mode="append")


# --- schema reflection / column validation --------------------------------


def test_load_raises_schema_mismatch_when_table_does_not_exist():
    connector = _connector()
    mock_engine = MagicMock()
    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch(
            "apps.connectors.destinations.sqlserver.sa.Table",
            side_effect=sa.exc.NoSuchTableError("policies"),
        ),
    ):
        with pytest.raises(SchemaMismatchError):
            connector.load(CREDENTIAL, [{"id": 1}], mode="append")

    mock_engine.dispose.assert_called_once()


def test_load_raises_schema_mismatch_on_unknown_column():
    connector = _connector()
    mock_engine = MagicMock()
    fake_table = _fake_table({"id", "name"})

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        with pytest.raises(SchemaMismatchError) as exc_info:
            connector.load(CREDENTIAL, [{"id": 1, "unmapped_column": "x"}], mode="append")

    assert exc_info.value.retryable is False
    mock_engine.dispose.assert_called_once()


# --- batching / successful load --------------------------------------------


def test_load_executes_bounded_batches_within_one_transaction():
    connector = _connector(batch_size=2)
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn
    fake_table = _fake_table({"id"})
    records = [{"id": i} for i in range(5)]

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        loaded = connector.load(CREDENTIAL, records, mode="append")

    assert loaded == 5
    assert mock_conn.execute.call_count == 3  # batches of 2, 2, 1
    mock_engine.begin.assert_called_once()  # one transaction for the whole call
    mock_engine.dispose.assert_called_once()


def test_load_rejects_invalid_batch_size():
    connector = _connector(batch_size=0)
    fake_table = _fake_table({"id"})
    with patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table):
        with pytest.raises(ConfigurationError):
            connector.load(CREDENTIAL, [{"id": 1}], mode="append")


# --- retryable classification -----------------------------------------


def test_load_classifies_operational_error_as_retryable():
    connector = _connector()
    mock_engine = MagicMock()
    mock_engine.begin.side_effect = sa.exc.OperationalError(
        "stmt", {}, Exception("connection lost")
    )
    fake_table = _fake_table({"id"})

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        with pytest.raises(LoadFailed) as exc_info:
            connector.load(CREDENTIAL, [{"id": 1}], mode="append")

    assert exc_info.value.retryable is True
    mock_engine.dispose.assert_called_once()


def test_load_classifies_integrity_error_as_non_retryable():
    connector = _connector()
    mock_engine = MagicMock()
    mock_engine.begin.side_effect = sa.exc.IntegrityError("stmt", {}, Exception("dup key"))
    fake_table = _fake_table({"id"})

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        with pytest.raises(LoadFailed) as exc_info:
            connector.load(CREDENTIAL, [{"id": 1}], mode="append")

    assert exc_info.value.retryable is False
    mock_engine.dispose.assert_called_once()


# --- engine disposal --------------------------------------------------


def test_engine_is_disposed_even_when_load_succeeds():
    connector = _connector()
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn
    fake_table = _fake_table({"id"})

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        connector.load(CREDENTIAL, [{"id": 1}], mode="append")

    mock_engine.dispose.assert_called_once()


# --- bulk_load(): Spark mode's faster destination write --------------


def test_capabilities_declare_bulk_load_support():
    assert SqlServerDestinationConnector.capabilities.supports_bulk_load is True


def test_bulk_load_rejects_unsupported_mode():
    connector = _connector()
    with pytest.raises(UnsupportedCapability):
        connector.bulk_load(CREDENTIAL, [{"id": 1}], mode="upsert")


def test_bulk_load_with_no_records_is_a_noop():
    connector = _connector()
    assert connector.bulk_load(CREDENTIAL, [], mode="append") == 0


def test_bulk_load_rejects_invalid_bulk_batch_size():
    connector = _connector(bulk_batch_size=0)
    fake_table = _fake_table({"id"})
    with patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table):
        with pytest.raises(ConfigurationError):
            connector.bulk_load(CREDENTIAL, [{"id": 1}], mode="append")


def test_bulk_load_stages_then_moves_data_in_one_transaction():
    connector = _connector(bulk_batch_size=2)
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn
    fake_table = _fake_table({"id"})
    staging_table = _fake_table({"id"})
    records = [{"id": i} for i in range(5)]

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
        patch("apps.connectors.destinations.sqlserver.sa.Table", return_value=staging_table),
    ):
        loaded = connector.bulk_load(CREDENTIAL, records, mode="append")

    assert loaded == 5
    # CREATE staging (1) + batched inserts into staging (2, 2, 1 -> 3) +
    # INSERT...SELECT into destination (1) + DROP staging (1) = 6.
    assert mock_conn.execute.call_count == 6
    mock_engine.begin.assert_called_once()
    mock_engine.dispose.assert_called_once()


def test_bulk_load_uses_a_freshly_generated_staging_table_name_each_call():
    connector = _connector()
    fake_table = _fake_table({"id"})
    staging_table = _fake_table({"id"})
    seen_staging_names = []

    def fake_sa_table(name, metadata, **kwargs):
        seen_staging_names.append(name)
        return staging_table

    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
        patch("apps.connectors.destinations.sqlserver.sa.Table", side_effect=fake_sa_table),
    ):
        connector.bulk_load(CREDENTIAL, [{"id": 1}], mode="append")
        connector.bulk_load(CREDENTIAL, [{"id": 2}], mode="append")

    assert len(seen_staging_names) == 2
    assert seen_staging_names[0] != seen_staging_names[1]
    for name in seen_staging_names:
        assert name.startswith("__bulk_staging_")


def test_bulk_load_classifies_operational_error_as_retryable():
    connector = _connector()
    mock_engine = MagicMock()
    mock_engine.begin.side_effect = sa.exc.OperationalError(
        "stmt", {}, Exception("connection lost")
    )
    fake_table = _fake_table({"id"})

    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", return_value=mock_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        with pytest.raises(LoadFailed) as exc_info:
            connector.bulk_load(CREDENTIAL, [{"id": 1}], mode="append")

    assert exc_info.value.retryable is True
    mock_engine.dispose.assert_called_once()


def test_engine_created_fresh_and_disposed_per_call():
    connector = _connector()
    mock_conn = MagicMock()
    engines_created = []

    def _new_engine(*args, **kwargs):
        engine = MagicMock()
        engine.begin.return_value.__enter__.return_value = mock_conn
        engines_created.append(engine)
        return engine

    fake_table = _fake_table({"id"})
    with (
        patch("apps.connectors.destinations.sqlserver.sa.create_engine", side_effect=_new_engine),
        patch.object(SqlServerDestinationConnector, "_reflect_table", return_value=fake_table),
    ):
        connector.load(CREDENTIAL, [{"id": 1}], mode="append")
        connector.load(CREDENTIAL, [{"id": 2}], mode="append")

    assert len(engines_created) == 2
    for engine in engines_created:
        engine.dispose.assert_called_once()
