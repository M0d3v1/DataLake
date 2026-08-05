"""Repo-wide pytest fixtures.

`apps.core.outbound_http.validate_outbound_url` performs a real DNS
resolution before allowing any outbound request. The test suite's REST
API / auth-provider examples all use fictional, non-resolvable hostnames
(the "*.example-insurer.test" convention used throughout), so without
this fixture every test that exercises a connector or auth provider
through respx would fail at the resolution step before respx ever gets a
chance to intercept the request.

This autouse fixture makes `resolve_host` return a fixed, ordinary public
address for any hostname, so existing tests don't need to know the
outbound policy exists. Tests that need to exercise the *real* blocking
behavior (loopback/link-local/private/etc.) override it locally -- see
apps/core/tests/test_outbound_http.py -- by patching `resolve_host`
themselves within that test, which simply shadows this fixture's effect
for the duration of that one test.
"""

import ipaddress

import pytest

_FAKE_PUBLIC_ADDRESS = ipaddress.ip_address("93.184.216.34")


@pytest.fixture(autouse=True)
def _fake_public_dns_resolution(monkeypatch):
    monkeypatch.setattr(
        "apps.core.outbound_http.resolve_host", lambda host: [_FAKE_PUBLIC_ADDRESS]
    )
