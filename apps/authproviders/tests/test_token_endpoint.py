import httpx
import pytest
import respx
from django.test import override_settings

from apps.authproviders.providers.token_endpoint import TokenEndpointAuthProvider
from apps.core.exceptions import AuthenticationError, ConfigurationError

TOKEN_URL = "https://idp.example-insurer.test/oauth/token"


def _dummy_request() -> httpx.Request:
    return httpx.Request("GET", "https://api.example-insurer.test/v1/policies")


def _credential(**overrides):
    credential = {
        "token_url": TOKEN_URL,
        "request_body": {"client_id": "demo", "client_secret": "demo-secret"},
        "extract": {"from": "json", "field": "access_token"},
    }
    credential.update(overrides)
    return credential


@respx.mock
def test_extracts_token_from_json_field_and_injects_bearer_header():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok_abc123"})
    )

    provider = TokenEndpointAuthProvider()
    request = provider.prepare_request(_dummy_request(), _credential())

    assert request.headers["Authorization"] == "Bearer tok_abc123"


@respx.mock
def test_extracts_token_from_response_header():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, headers={"X-Access-Token": "tok_from_header"}, json={})
    )

    provider = TokenEndpointAuthProvider()
    credential = _credential(extract={"from": "header", "header": "X-Access-Token"})
    request = provider.prepare_request(_dummy_request(), credential)

    assert request.headers["Authorization"] == "Bearer tok_from_header"


@respx.mock
def test_reuses_cached_token_across_multiple_prepare_request_calls():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok_abc123"})
    )

    provider = TokenEndpointAuthProvider()
    credential = _credential()
    provider.prepare_request(_dummy_request(), credential)
    provider.prepare_request(_dummy_request(), credential)
    provider.prepare_request(_dummy_request(), credential)

    assert route.call_count == 1


@respx.mock
def test_invalidate_forces_reacquisition():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok_abc123"})
    )

    provider = TokenEndpointAuthProvider()
    credential = _credential()
    provider.prepare_request(_dummy_request(), credential)
    provider.invalidate(credential)
    provider.prepare_request(_dummy_request(), credential)

    assert route.call_count == 2


@respx.mock
def test_reacquires_when_token_expired():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok_abc123", "expires_in": 0})
    )

    provider = TokenEndpointAuthProvider()
    credential = _credential(expires_in_field="expires_in")
    provider.prepare_request(_dummy_request(), credential)
    provider.prepare_request(_dummy_request(), credential)

    assert route.call_count == 2


@respx.mock
def test_custom_inject_header_and_prefix():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok_x"}))

    provider = TokenEndpointAuthProvider()
    credential = _credential(inject_header="X-Auth", inject_prefix="Token ")
    request = provider.prepare_request(_dummy_request(), credential)

    assert request.headers["X-Auth"] == "Token tok_x"
    assert "Authorization" not in request.headers


@respx.mock
def test_raises_authentication_error_on_token_endpoint_http_error():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

    provider = TokenEndpointAuthProvider()
    with pytest.raises(AuthenticationError):
        provider.prepare_request(_dummy_request(), _credential())


def test_requires_absolute_token_url():
    provider = TokenEndpointAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), _credential(token_url="/relative/path"))


def test_requires_extract_config():
    provider = TokenEndpointAuthProvider()
    credential = _credential()
    del credential["extract"]
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), credential)


def test_rejects_excessive_timeout():
    provider = TokenEndpointAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), _credential(timeout_seconds=9999))


@override_settings(OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS=30)
def test_timeout_ceiling_is_deployment_configurable_not_hardcoded():
    # item 3: authentication calls must respect the same deployment-wide
    # timeout ceiling as source connections, not a separate hardcoded
    # constant that ignores settings.OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS.
    provider = TokenEndpointAuthProvider()
    with pytest.raises(ConfigurationError, match="30"):
        provider.prepare_request(_dummy_request(), _credential(timeout_seconds=60))


@respx.mock
@override_settings(OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS=600)
def test_a_higher_deployment_ceiling_allows_a_previously_rejected_timeout():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok_abc123"})
    )
    provider = TokenEndpointAuthProvider()

    request = provider.prepare_request(_dummy_request(), _credential(timeout_seconds=300))

    assert request.headers["Authorization"] == "Bearer tok_abc123"


@respx.mock
def test_never_puts_token_value_in_a_raised_error_message():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))

    provider = TokenEndpointAuthProvider()
    with pytest.raises(AuthenticationError) as exc_info:
        provider.prepare_request(_dummy_request(), _credential())

    assert "demo-secret" not in str(exc_info.value)
