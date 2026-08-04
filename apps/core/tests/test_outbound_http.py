import ipaddress

import httpx
import pytest
import respx
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.core.exceptions import OutboundRequestBlockedError
from apps.core.outbound_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    build_client,
    guarded_send,
    read_bounded_body,
    resolve_max_response_bytes,
    resolve_timeout_seconds,
    validate_outbound_url,
)


def _mock_resolve(monkeypatch, *addresses: str):
    ips = [ipaddress.ip_address(a) for a in addresses]
    monkeypatch.setattr("apps.core.outbound_http.resolve_host", lambda host: ips)


# --- scheme / credentials-in-url --------------------------------------


def test_rejects_non_http_scheme():
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("ftp://api.example-insurer.test/data")


def test_rejects_credentials_embedded_in_url(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("https://user:pass@api.example-insurer.test/data")


def test_rejects_plain_http_by_default(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("http://api.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_ALLOW_INSECURE_HTTP=True)
def test_allows_plain_http_when_explicitly_enabled(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    validate_outbound_url("http://api.example-insurer.test/data")  # must not raise


# --- blocked address categories ----------------------------------------


def test_rejects_loopback_address(monkeypatch):
    _mock_resolve(monkeypatch, "127.0.0.1")
    with pytest.raises(OutboundRequestBlockedError, match="loopback"):
        validate_outbound_url("https://internal.example-insurer.test/data")


def test_rejects_link_local_address(monkeypatch):
    _mock_resolve(monkeypatch, "169.254.1.1")
    with pytest.raises(OutboundRequestBlockedError, match="link-local"):
        validate_outbound_url("https://internal.example-insurer.test/data")


def test_rejects_cloud_metadata_address(monkeypatch):
    # 169.254.169.254 -- the link-local address every major cloud uses
    # for its instance metadata service. Blocked as link-local, which is
    # exactly the point: no separate "cloud metadata" carve-out is needed.
    _mock_resolve(monkeypatch, "169.254.169.254")
    with pytest.raises(OutboundRequestBlockedError, match="link-local"):
        validate_outbound_url("https://internal.example-insurer.test/data")


def test_rejects_multicast_address(monkeypatch):
    _mock_resolve(monkeypatch, "224.0.0.1")
    with pytest.raises(OutboundRequestBlockedError, match="multicast"):
        validate_outbound_url("https://internal.example-insurer.test/data")


def test_rejects_unspecified_address(monkeypatch):
    _mock_resolve(monkeypatch, "0.0.0.0")
    with pytest.raises(OutboundRequestBlockedError, match="unspecified"):
        validate_outbound_url("https://internal.example-insurer.test/data")


def test_rejects_private_address(monkeypatch):
    _mock_resolve(monkeypatch, "10.0.0.5")
    with pytest.raises(OutboundRequestBlockedError, match="private"):
        validate_outbound_url("https://internal.example-insurer.test/data")


def test_rejects_ipv6_loopback_and_link_local(monkeypatch):
    _mock_resolve(monkeypatch, "::1")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("https://internal.example-insurer.test/data")

    _mock_resolve(monkeypatch, "fe80::1")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("https://internal.example-insurer.test/data")


# --- allowed public hosts -----------------------------------------------


def test_allows_ordinary_public_host(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    validate_outbound_url("https://api.example-insurer.test/v1/policies")  # must not raise


# --- allowlisting ---------------------------------------------------------


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"])
def test_deployment_allowlist_permits_a_private_host(monkeypatch):
    _mock_resolve(monkeypatch, "10.0.0.5")
    validate_outbound_url("https://internal.example-insurer.test/data")  # must not raise


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["INTERNAL.example-insurer.test"])
def test_deployment_allowlist_is_case_insensitive(monkeypatch):
    _mock_resolve(monkeypatch, "10.0.0.5")
    validate_outbound_url("https://internal.example-insurer.test/data")  # must not raise


def test_allowlist_does_not_bypass_scheme_validation(monkeypatch):
    with override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"]):
        with pytest.raises(OutboundRequestBlockedError):
            validate_outbound_url("ftp://internal.example-insurer.test/data")


# --- item 2: allowlist semantics are layered, not one flag -------------


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"])
def test_private_allowlist_does_not_implicitly_permit_plain_http(monkeypatch):
    # Being allowlisted for a private *address* must not also grant an
    # http:// scheme exception -- that's the exact bug item 2 fixes.
    _mock_resolve(monkeypatch, "10.0.0.5")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("http://internal.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_INSECURE_ALLOWED_HOSTS=["internal.example-insurer.test"])
def test_insecure_allowed_hosts_permits_http_for_that_host_only(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    validate_outbound_url("http://internal.example-insurer.test/data")  # must not raise


@override_settings(OUTBOUND_HTTP_INSECURE_ALLOWED_HOSTS=["other.example-insurer.test"])
def test_insecure_allowed_hosts_does_not_permit_http_for_a_different_host(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("http://internal.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"])
def test_private_allowlist_does_not_bypass_loopback(monkeypatch):
    _mock_resolve(monkeypatch, "127.0.0.1")
    with pytest.raises(OutboundRequestBlockedError, match="loopback"):
        validate_outbound_url("https://internal.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"])
def test_private_allowlist_does_not_bypass_link_local(monkeypatch):
    _mock_resolve(monkeypatch, "169.254.169.254")
    with pytest.raises(OutboundRequestBlockedError, match="link-local"):
        validate_outbound_url("https://internal.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"])
def test_private_allowlist_does_not_bypass_multicast(monkeypatch):
    _mock_resolve(monkeypatch, "224.0.0.1")
    with pytest.raises(OutboundRequestBlockedError, match="multicast"):
        validate_outbound_url("https://internal.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS=["internal.example-insurer.test"])
def test_private_allowlist_does_not_bypass_unspecified(monkeypatch):
    _mock_resolve(monkeypatch, "0.0.0.0")
    with pytest.raises(OutboundRequestBlockedError, match="unspecified"):
        validate_outbound_url("https://internal.example-insurer.test/data")


@override_settings(OUTBOUND_HTTP_ALLOWED_UNSAFE_HOSTS=["metadata.example-insurer.test"])
def test_unsafe_allowlist_permits_loopback(monkeypatch):
    _mock_resolve(monkeypatch, "127.0.0.1")
    validate_outbound_url("https://metadata.example-insurer.test/data")  # must not raise


@override_settings(OUTBOUND_HTTP_ALLOWED_UNSAFE_HOSTS=["metadata.example-insurer.test"])
def test_unsafe_allowlist_permits_link_local_cloud_metadata(monkeypatch):
    _mock_resolve(monkeypatch, "169.254.169.254")
    validate_outbound_url("https://metadata.example-insurer.test/data")  # must not raise


@override_settings(OUTBOUND_HTTP_ALLOWED_UNSAFE_HOSTS=["metadata.example-insurer.test"])
def test_unsafe_allowlist_still_requires_https_separately(monkeypatch):
    _mock_resolve(monkeypatch, "127.0.0.1")
    with pytest.raises(OutboundRequestBlockedError):
        validate_outbound_url("http://metadata.example-insurer.test/data")


class TenantAllowlistNeverBypassesUnsafeCategoriesTests(TenantTestCase):
    def test_tenant_allowlist_does_not_bypass_loopback(self):
        from apps.connections.models import AllowedOutboundHost

        AllowedOutboundHost.objects.create(host="internal.example-insurer.test", reason="test")

        import ipaddress as _ip
        from unittest.mock import patch

        with patch(
            "apps.core.outbound_http.resolve_host",
            return_value=[_ip.ip_address("127.0.0.1")],
        ):
            with pytest.raises(OutboundRequestBlockedError, match="loopback"):
                validate_outbound_url("https://internal.example-insurer.test/data")

    def test_tenant_allowlist_does_not_bypass_link_local_cloud_metadata(self):
        from apps.connections.models import AllowedOutboundHost

        AllowedOutboundHost.objects.create(host="internal.example-insurer.test", reason="test")

        import ipaddress as _ip
        from unittest.mock import patch

        with patch(
            "apps.core.outbound_http.resolve_host",
            return_value=[_ip.ip_address("169.254.169.254")],
        ):
            with pytest.raises(OutboundRequestBlockedError, match="link-local"):
                validate_outbound_url("https://internal.example-insurer.test/data")

    def test_tenant_allowlist_does_not_grant_http_scheme_permission(self):
        from apps.connections.models import AllowedOutboundHost

        AllowedOutboundHost.objects.create(host="internal.example-insurer.test", reason="test")

        import ipaddress as _ip
        from unittest.mock import patch

        with patch(
            "apps.core.outbound_http.resolve_host",
            return_value=[_ip.ip_address("10.0.0.5")],
        ):
            with pytest.raises(OutboundRequestBlockedError):
                validate_outbound_url("http://internal.example-insurer.test/data")


# --- item 3: deployment-level hard ceilings -----------------------------


def test_resolve_max_response_bytes_uses_the_deployment_ceiling_when_no_request():
    assert resolve_max_response_bytes() == DEFAULT_MAX_RESPONSE_BYTES


def test_resolve_max_response_bytes_allows_a_lower_connector_request():
    assert resolve_max_response_bytes(1000) == 1000


@override_settings(OUTBOUND_HTTP_MAX_RESPONSE_BYTES=5000)
def test_resolve_max_response_bytes_clamps_a_higher_connector_request_to_the_ceiling():
    assert resolve_max_response_bytes(10_000_000) == 5000


def test_resolve_timeout_seconds_uses_the_deployment_ceiling_when_no_request():
    assert resolve_timeout_seconds() == DEFAULT_MAX_TIMEOUT_SECONDS


def test_resolve_timeout_seconds_allows_a_lower_connector_request():
    assert resolve_timeout_seconds(5) == 5


@override_settings(OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS=60)
def test_resolve_timeout_seconds_clamps_a_higher_connector_request_to_the_ceiling():
    assert resolve_timeout_seconds(600) == 60


@respx.mock
@override_settings(OUTBOUND_HTTP_MAX_RESPONSE_BYTES=100)
def test_guarded_send_enforces_the_deployment_ceiling_even_when_caller_requests_more(monkeypatch):
    # A connector passing a higher max_response_bytes than the deployment
    # allows must not be able to override the deployment's hard ceiling.
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(
        return_value=httpx.Response(200, content=b"y" * 1000)
    )

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        with pytest.raises(OutboundRequestBlockedError):
            guarded_send(client, request, max_response_bytes=1_000_000)


class TenantAllowlistTests(TenantTestCase):
    def test_tenant_allowlist_permits_a_private_host(self):
        from apps.connections.models import AllowedOutboundHost

        AllowedOutboundHost.objects.create(host="internal.example-insurer.test", reason="test")

        import ipaddress as _ip
        from unittest.mock import patch

        with patch(
            "apps.core.outbound_http.resolve_host",
            return_value=[_ip.ip_address("10.0.0.5")],
        ):
            validate_outbound_url("https://internal.example-insurer.test/data")  # must not raise

    def test_tenant_allowlist_is_scoped_by_host_only_this_org_added(self):
        from apps.connections.models import AllowedOutboundHost

        # A different host was never added -- still blocked.
        AllowedOutboundHost.objects.create(host="other-internal.example-insurer.test")

        import ipaddress as _ip
        from unittest.mock import patch

        with patch(
            "apps.core.outbound_http.resolve_host",
            return_value=[_ip.ip_address("10.0.0.5")],
        ):
            with pytest.raises(OutboundRequestBlockedError):
                validate_outbound_url("https://internal.example-insurer.test/data")


# --- redirects --------------------------------------------------------


@respx.mock
def test_guarded_send_blocks_redirects(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(
        return_value=httpx.Response(302, headers={"location": "https://attacker.example-insurer.test/"})
    )
    # attacker.example-insurer.test is deliberately NOT mocked -- if the
    # redirect were followed, respx would raise for the unmocked route.

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        with pytest.raises(OutboundRequestBlockedError):
            guarded_send(client, request)


# --- oversized responses -----------------------------------------------


def test_read_bounded_body_aborts_on_oversized_response():
    response = httpx.Response(200, content=b"x" * 1000)
    with pytest.raises(OutboundRequestBlockedError):
        read_bounded_body(response, max_bytes=100)


def test_read_bounded_body_allows_response_within_the_cap():
    response = httpx.Response(200, content=b"x" * 100)
    body = read_bounded_body(response, max_bytes=1000)
    assert body == b"x" * 100


@respx.mock
def test_guarded_send_enforces_response_size_cap(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(
        return_value=httpx.Response(200, content=b"y" * 1000)
    )

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        with pytest.raises(OutboundRequestBlockedError):
            guarded_send(client, request, max_response_bytes=100)


@respx.mock
def test_guarded_send_returns_body_within_the_cap(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        response, body = guarded_send(client, request)

    import json as jsonlib

    assert response.status_code == 200
    assert jsonlib.loads(body) == {"ok": True}


def test_default_max_response_bytes_is_bounded():
    assert 0 < DEFAULT_MAX_RESPONSE_BYTES <= 100 * 1024 * 1024


# --- client construction -------------------------------------------------


def test_build_client_disables_redirects_by_default():
    with build_client() as client:
        assert client.follow_redirects is False


def test_build_client_uses_bounded_timeouts():
    with build_client() as client:
        timeout = client.timeout
        assert timeout.connect is not None
        assert timeout.read is not None
        assert timeout.write is not None
        assert timeout.pool is not None


def test_build_client_does_not_trust_environment_proxy_settings():
    # item 4: any proxy support must be explicit/deployment-controlled,
    # never silently picked up from HTTP_PROXY/.netrc in the process env.
    with build_client() as client:
        assert client.trust_env is False


def test_build_client_caller_can_still_override_trust_env_explicitly():
    with build_client(trust_env=True) as client:
        assert client.trust_env is True


# --- item 7: redirect Location headers are sanitized before use --------


@respx.mock
def test_guarded_send_strips_query_string_and_fragment_from_redirect_location(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(
        return_value=httpx.Response(
            302,
            headers={
                "location": (
                    "https://attacker.example-insurer.test/steal"
                    "?token=super-secret-value&session=abc123#fragment-secret"
                )
            },
        )
    )

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        with pytest.raises(OutboundRequestBlockedError) as exc_info:
            guarded_send(client, request)

    message = str(exc_info.value)
    assert "super-secret-value" not in message
    assert "session=abc123" not in message
    assert "fragment-secret" not in message
    assert "attacker.example-insurer.test" in message  # host itself is still useful to log


@respx.mock
def test_guarded_send_strips_credentials_embedded_in_redirect_location(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://leaked-user:leaked-pass@attacker.example-insurer.test/"},
        )
    )

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        with pytest.raises(OutboundRequestBlockedError) as exc_info:
            guarded_send(client, request)

    message = str(exc_info.value)
    assert "leaked-user" not in message
    assert "leaked-pass" not in message


@respx.mock
def test_guarded_send_handles_a_missing_or_unparseable_redirect_location(monkeypatch):
    _mock_resolve(monkeypatch, "93.184.216.34")
    respx.get("https://api.example-insurer.test/data").mock(return_value=httpx.Response(302))

    with build_client() as client:
        request = client.build_request("GET", "https://api.example-insurer.test/data")
        with pytest.raises(OutboundRequestBlockedError):
            guarded_send(client, request)
