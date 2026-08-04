from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from django.test import override_settings

from apps.connectors.sources.rest_api import RestApiSourceConnector
from apps.core.exceptions import (
    ConfigurationError,
    ConnectionTestFailed,
    FetchFailed,
    MalformedResponseError,
    PageLimitExceededError,
)

BASE_URL = "https://api.example-insurer.test"


def _connector(**config_overrides):
    config = {"base_url": BASE_URL, "path": "/v1/policies"}
    config.update(config_overrides)
    return RestApiSourceConnector(config)


# --- pagination -------------------------------------------------------


@respx.mock
def test_fetch_uses_page_size_to_detect_the_last_page_without_an_extra_request():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1", "page_size": "2"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}, {"id": 2}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2", "page_size": "2"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 3}]})
    )
    # page 3 is deliberately unmocked: a short page (1 < page_size=2)
    # must stop pagination without an extra request.

    pages = list(_connector(page_size=2).fetch(credential={}))

    assert len(pages) == 2
    assert pages[0].records == [{"id": 1}, {"id": 2}]
    assert pages[0].next_cursor == "2"
    assert pages[1].records == [{"id": 3}]
    assert pages[1].next_cursor is None


@respx.mock
def test_fetch_does_not_yield_an_empty_terminal_page_by_default():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    pages = list(_connector().fetch(credential={}))

    assert len(pages) == 1
    assert pages[0].records == [{"id": 1}]


@respx.mock
def test_fetch_retains_empty_terminal_page_when_configured():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    pages = list(_connector(retain_empty_terminal_page=True).fetch(credential={}))

    assert len(pages) == 2
    assert pages[1].records == []
    assert pages[1].next_cursor is None


@respx.mock
def test_fetch_raises_page_limit_exceeded_when_more_data_may_remain():
    for page in (1, 2, 3):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": str(page)}).mock(
            return_value=httpx.Response(200, json={"results": [{"id": page}]})
        )
    # page 4 deliberately unmocked -- max_pages=3 must stop before requesting it.

    pages = []
    with pytest.raises(PageLimitExceededError) as exc_info:
        for page in _connector(max_pages=3).fetch(credential={}):
            pages.append(page)

    # the pages up to the limit were still yielded (a caller processes
    # and persists them) before the connector raises.
    assert len(pages) == 3
    assert [p.records[0]["id"] for p in pages] == [1, 2, 3]
    assert exc_info.value.retryable is False
    message = str(exc_info.value)
    assert "max_pages=3" in message
    assert "3" in message  # last processed page number, in some form


@respx.mock
def test_fetch_max_pages_error_never_includes_response_body():
    for page in (1, 2, 3):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": str(page)}).mock(
            return_value=httpx.Response(
                200, json={"results": [{"id": page, "ssn": "secret-body-marker"}]}
            )
        )

    with pytest.raises(PageLimitExceededError) as exc_info:
        list(_connector(max_pages=3).fetch(credential={}))

    assert "secret-body-marker" not in str(exc_info.value)


@respx.mock
def test_fetch_natural_empty_page_termination_does_not_raise_even_near_the_limit():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    # The limit is only ever checked right after a *non-terminal* page is
    # yielded; a naturally empty page short-circuits before that check,
    # even with a tight max_pages, so this must not raise.
    pages = list(_connector(max_pages=2).fetch(credential={}))

    assert len(pages) == 1


@respx.mock
def test_fetch_short_page_termination_does_not_raise_even_at_the_limit():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1", "page_size": "5"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}, {"id": 2}]})
    )

    # A short page (fewer records than page_size) is a real end of data
    # even though it's also exactly at max_pages=1 -- must not raise.
    pages = list(_connector(max_pages=1, page_size=5).fetch(credential={}))

    assert len(pages) == 1
    assert pages[0].next_cursor is None


@respx.mock
def test_fetch_honors_start_page():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "5"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    list(_connector(start_page=5).fetch(credential={}))
    # no assertion needed beyond respx not raising for an unmocked route


@respx.mock
def test_fetch_resumes_from_cursor():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "3"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    list(_connector().fetch(credential={}, cursor="3"))


# --- request construction ----------------------------------------------


@respx.mock
def test_fetch_sends_static_query_params_and_headers():
    route = respx.get(f"{BASE_URL}/v1/policies", params={"page": "1", "region": "north"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    connector = _connector(query_params={"region": "north"}, headers={"X-Client": "datalake"})
    list(connector.fetch(credential={}))

    assert route.calls[0].request.headers["X-Client"] == "datalake"


@respx.mock
def test_fetch_supports_post_with_json_body():
    route = respx.post(f"{BASE_URL}/v1/policies/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    connector = _connector(
        path="/v1/policies/search", method="POST", json_body={"status": "active"}
    )
    list(connector.fetch(credential={}))

    import json as jsonlib

    assert jsonlib.loads(route.calls[0].request.content) == {"status": "active"}


@respx.mock
def test_fetch_supports_nested_records_path():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"data": {"items": [{"id": 1}]}})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"data": {"items": []}})
    )

    pages = list(_connector(records_path="data.items").fetch(credential={}))

    assert len(pages) == 1
    assert pages[0].records == [{"id": 1}]


# --- malformed responses -------------------------------------------------


@respx.mock
def test_fetch_raises_on_invalid_json():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}
        )
    )

    with pytest.raises(MalformedResponseError):
        list(_connector().fetch(credential={}))


@respx.mock
def test_fetch_raises_when_records_field_is_missing():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(MalformedResponseError):
        list(_connector().fetch(credential={}))


@respx.mock
def test_fetch_raises_when_records_field_is_not_a_list():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": "not-a-list"})
    )

    with pytest.raises(MalformedResponseError):
        list(_connector().fetch(credential={}))


def test_malformed_response_error_is_never_retryable():
    respx_route_error = MalformedResponseError("bad shape")
    assert respx_route_error.retryable is False


# --- HTTP failure classification -----------------------------------------


@respx.mock
def test_fetch_classifies_5xx_as_retryable():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(FetchFailed) as exc_info:
        list(_connector().fetch(credential={}))
    assert exc_info.value.retryable is True


@respx.mock
def test_fetch_classifies_429_as_retryable():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(429)
    )

    with pytest.raises(FetchFailed) as exc_info:
        list(_connector().fetch(credential={}))
    assert exc_info.value.retryable is True


@respx.mock
def test_fetch_classifies_4xx_as_non_retryable():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(FetchFailed) as exc_info:
        list(_connector().fetch(credential={}))
    assert exc_info.value.retryable is False


@respx.mock
def test_fetch_classifies_network_timeout_as_retryable():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )

    with pytest.raises(FetchFailed) as exc_info:
        list(_connector().fetch(credential={}))
    assert exc_info.value.retryable is True


@respx.mock
def test_test_connection_raises_connection_test_failed_on_http_error():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(401)
    )

    with pytest.raises(ConnectionTestFailed):
        _connector().test_connection(credential={})


# --- authentication integration ------------------------------------------


@respx.mock
def test_fetch_applies_configured_auth_provider():
    route = respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    connector = _connector(auth_provider_type="bearer")
    list(connector.fetch(credential={"token": "tok_abc"}))

    assert route.calls[0].request.headers["Authorization"] == "Bearer tok_abc"


@respx.mock
def test_fetch_resolves_auth_provider_once_per_call_not_once_per_page():
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": 2}]})
    )
    respx.get(f"{BASE_URL}/v1/policies", params={"page": "3"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    fake_provider = MagicMock()
    fake_provider.prepare_request.side_effect = lambda request, credential: request
    with patch(
        "apps.connectors.sources.rest_api.get_auth_provider", return_value=fake_provider
    ) as get_provider:
        list(_connector(auth_provider_type="bearer").fetch(credential={"token": "t"}))

    assert get_provider.call_count == 1
    assert fake_provider.prepare_request.call_count == 3


@respx.mock
def test_fetch_reauthenticates_once_on_401_then_succeeds():
    route = respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"})
    route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"results": []}),
    ]

    connector = _connector(auth_provider_type="bearer")
    pages = list(connector.fetch(credential={"token": "tok_abc"}))

    assert route.call_count == 2
    assert pages == []  # empty terminal page not retained by default


# --- config validation -----------------------------------------------------


def test_rejects_unsupported_method():
    with pytest.raises(ConfigurationError):
        list(_connector(method="DELETE").fetch(credential={}))


def test_rejects_non_positive_max_pages():
    with pytest.raises(ConfigurationError):
        list(_connector(max_pages=0).fetch(credential={}))


def test_rejects_non_positive_timeout():
    with pytest.raises(ConfigurationError):
        list(_connector(timeout_seconds=-1).fetch(credential={}))


def test_requires_base_url():
    with pytest.raises(ConfigurationError):
        list(RestApiSourceConnector({"path": "/v1/policies"}).fetch(credential={}))


# --- item 3: deployment-level hard ceilings -----------------------------


@override_settings(OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS=15)
def test_client_read_timeout_is_clamped_to_the_deployment_ceiling():
    connector = _connector(timeout_seconds=9999)
    with connector._client() as client:
        assert client.timeout.read == 15


def test_client_read_timeout_below_the_ceiling_is_used_as_requested():
    connector = _connector(timeout_seconds=5)
    with connector._client() as client:
        assert client.timeout.read == 5


@override_settings(OUTBOUND_HTTP_MAX_RESPONSE_BYTES=1000)
def test_max_response_bytes_is_clamped_to_the_deployment_ceiling():
    connector = _connector(max_response_bytes=10_000_000)
    assert connector._max_response_bytes() == 1000


def test_max_response_bytes_below_the_ceiling_is_used_as_requested():
    connector = _connector(max_response_bytes=500)
    assert connector._max_response_bytes() == 500
