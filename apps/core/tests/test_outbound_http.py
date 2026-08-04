import ipaddress

import httpx
import pytest
import respx
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.core.exceptions import OutboundRequestBlockedError
from apps.core.outbound_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    build_client,
    guarded_send,
    read_bounded_body,
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
