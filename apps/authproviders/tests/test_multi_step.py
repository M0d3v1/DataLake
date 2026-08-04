import httpx
import pytest
import respx

from apps.authproviders.providers.multi_step import MultiStepTokenAuthProvider
from apps.core.exceptions import AuthenticationError, ConfigurationError

STEP1_URL = "https://idp.example-insurer.test/step1"
STEP2_URL = "https://idp.example-insurer.test/step2"


def _dummy_request() -> httpx.Request:
    return httpx.Request("GET", "https://api.example-insurer.test/v1/policies")


def _credential(**overrides):
    credential = {
        "client_id": "demo-client",
        "client_secret": "demo-secret",
        "steps": [
            {
                "method": "POST",
                "url": STEP1_URL,
                "json_body": {
                    "client_id": "{{credential.client_id}}",
                    "client_secret": "{{credential.client_secret}}",
                },
                "extract": {"as": "intermediate_token", "from": "json", "field": "token"},
            },
            {
                "method": "POST",
                "url": STEP2_URL,
                "headers": {"X-Intermediate-Token": "{{steps.intermediate_token}}"},
                "extract": {"as": "access_token", "from": "json", "field": "access_token"},
            },
        ],
        "final_token_from": "access_token",
    }
    credential.update(overrides)
    return credential


@respx.mock
def test_bounded_multi_step_flow_produces_final_token():
    step1 = respx.post(STEP1_URL).mock(
        return_value=httpx.Response(200, json={"token": "intermediate_xyz"})
    )
    step2 = respx.post(STEP2_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "final_abc"})
    )

    provider = MultiStepTokenAuthProvider()
    request = provider.prepare_request(_dummy_request(), _credential())

    assert request.headers["Authorization"] == "Bearer final_abc"
    assert step1.calls[0].request.method == "POST"
    # step 2 must have received the value step 1 produced, substituted safely
    assert step2.calls[0].request.headers["X-Intermediate-Token"] == "intermediate_xyz"


@respx.mock
def test_placeholder_substitution_uses_credential_values_not_literal_text():
    step1 = respx.post(STEP1_URL).mock(return_value=httpx.Response(200, json={"token": "t"}))
    respx.post(STEP2_URL).mock(return_value=httpx.Response(200, json={"access_token": "final"}))

    provider = MultiStepTokenAuthProvider()
    provider.prepare_request(_dummy_request(), _credential())

    import json as jsonlib

    body = jsonlib.loads(step1.calls[0].request.content)
    assert body == {"client_id": "demo-client", "client_secret": "demo-secret"}


@respx.mock
def test_caches_final_token_across_calls():
    step1 = respx.post(STEP1_URL).mock(return_value=httpx.Response(200, json={"token": "t"}))
    step2 = respx.post(STEP2_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "final"})
    )

    provider = MultiStepTokenAuthProvider()
    credential = _credential()
    provider.prepare_request(_dummy_request(), credential)
    provider.prepare_request(_dummy_request(), credential)

    assert step1.call_count == 1
    assert step2.call_count == 1


def test_rejects_more_than_five_steps():
    credential = _credential()
    credential["steps"] = credential["steps"] * 3  # 6 steps
    provider = MultiStepTokenAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), credential)


def test_rejects_empty_steps():
    provider = MultiStepTokenAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), _credential(steps=[]))


def test_requires_final_token_from():
    credential = _credential()
    del credential["final_token_from"]
    provider = MultiStepTokenAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), credential)


def test_requires_absolute_url_per_step():
    credential = _credential()
    credential["steps"][0]["url"] = "/relative"
    provider = MultiStepTokenAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), credential)


def test_unresolved_placeholder_is_a_configuration_error_not_sent_literally():
    credential = _credential()
    credential["steps"][0]["json_body"] = {"x": "{{credential.does_not_exist}}"}
    provider = MultiStepTokenAuthProvider()
    with pytest.raises(ConfigurationError):
        provider.prepare_request(_dummy_request(), credential)


@respx.mock
def test_raises_authentication_error_when_final_token_never_produced():
    respx.post(STEP1_URL).mock(return_value=httpx.Response(200, json={"token": "t"}))
    respx.post(STEP2_URL).mock(return_value=httpx.Response(200, json={"nope": "wrong field"}))

    provider = MultiStepTokenAuthProvider()
    with pytest.raises(AuthenticationError):
        provider.prepare_request(_dummy_request(), _credential())


@respx.mock
def test_never_puts_client_secret_in_a_raised_error_message():
    respx.post(STEP1_URL).mock(return_value=httpx.Response(500))

    provider = MultiStepTokenAuthProvider()
    with pytest.raises(AuthenticationError) as exc_info:
        provider.prepare_request(_dummy_request(), _credential())

    assert "demo-secret" not in str(exc_info.value)
