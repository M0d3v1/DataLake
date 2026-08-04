from apps.connectors.registry import (
    available_destination_types,
    available_source_types,
    get_destination_connector,
    get_source_connector,
)


def test_rest_api_source_is_registered():
    assert "rest_api" in available_source_types()
    connector = get_source_connector("rest_api", {"base_url": "https://x.test", "path": "/y"})
    assert connector.type_key == "rest_api"


def test_sqlserver_destination_is_registered():
    assert "sqlserver" in available_destination_types()
    connector = get_destination_connector(
        "sqlserver", {"host": "x.test", "database": "d", "table_name": "t"}
    )
    assert connector.type_key == "sqlserver"
