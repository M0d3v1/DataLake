import httpx
import pytest
import respx

from apps.connectors.sources.rest_api import RestApiSourceConnector
from apps.core.exceptions import ConnectionTestFailed, FetchFailed

BASE_URL = "https://api.example-insurer.test"


def _connector(**config_overrides):
    config = {"base_url": BASE_URL, "path": "/v1/policies"}
    config.update(config_overrides)
    return RestApiSourceConnector(config)


@respx.mock
def test_fetch_paginates_until_an_empty_page():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}, {"id": 2}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    pages = list(_connector().fetch(credential={}))

    assert len(pages) == 2
    assert pages[0].records == [{"id": 1}, {"id": 2}]
    assert pages[0].next_cursor == "2"
    assert pages[1].records == []
    assert pages[1].next_cursor is None


@respx.mock
def test_fetch_applies_configured_auth_provider():
    route = respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    connector = _connector(auth_provider_type="bearer")
    list(connector.fetch(credential={"token": "tok_abc"}))

    assert route.calls[0].request.headers["Authorization"] == "Bearer tok_abc"


@respx.mock
def test_test_connection_raises_on_http_error():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(401)
    )

    with pytest.raises(ConnectionTestFailed):
        _connector().test_connection(credential={})


@respx.mock
def test_fetch_raises_fetch_failed_on_http_error():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(FetchFailed):
        list(_connector().fetch(credential={}))
