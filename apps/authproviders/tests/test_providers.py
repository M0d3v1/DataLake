import base64

import httpx
import pytest

from apps.authproviders.providers.api_key import ApiKeyAuthProvider
from apps.authproviders.providers.basic import BasicAuthProvider
from apps.authproviders.providers.bearer import BearerAuthProvider
from apps.authproviders.registry import available_auth_provider_types, get_auth_provider
from apps.core.exceptions import AuthenticationError


def _dummy_request() -> httpx.Request:
    return httpx.Request("GET", "https://api.example-insurer.test/v1/policies")


def test_api_key_provider_sets_default_header():
    request = ApiKeyAuthProvider().prepare_request(_dummy_request(), {"api_key": "abc123"})
    assert request.headers["X-API-Key"] == "abc123"


def test_api_key_provider_honors_custom_header_name():
    request = ApiKeyAuthProvider().prepare_request(
        _dummy_request(), {"api_key": "abc123", "header_name": "X-Custom-Key"}
    )
    assert request.headers["X-Custom-Key"] == "abc123"
    assert "X-API-Key" not in request.headers


def test_api_key_provider_requires_api_key():
    with pytest.raises(AuthenticationError):
        ApiKeyAuthProvider().prepare_request(_dummy_request(), {})


def test_bearer_provider_sets_authorization_header():
    request = BearerAuthProvider().prepare_request(_dummy_request(), {"token": "tok_123"})
    assert request.headers["Authorization"] == "Bearer tok_123"


def test_bearer_provider_requires_token():
    with pytest.raises(AuthenticationError):
        BearerAuthProvider().prepare_request(_dummy_request(), {})


def test_basic_provider_encodes_credentials():
    request = BasicAuthProvider().prepare_request(
        _dummy_request(), {"username": "svc", "password": "pw"}
    )
    expected = "Basic " + base64.b64encode(b"svc:pw").decode()
    assert request.headers["Authorization"] == expected


def test_registry_contains_builtin_providers():
    types = available_auth_provider_types()
    for expected in ["api_key", "basic", "bearer", "token_endpoint", "multi_step_token"]:
        assert expected in types
    assert isinstance(get_auth_provider("bearer"), BearerAuthProvider)


def test_unimplemented_providers_raise_not_implemented():
    provider = get_auth_provider("token_endpoint")
    with pytest.raises(NotImplementedError):
        provider.prepare_request(_dummy_request(), {})
