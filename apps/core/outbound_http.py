"""Shared outbound HTTP security policy.

Every outbound call this platform makes to a tenant-configured URL --
REST source requests, token-endpoint and multi-step authentication
requests, and connection tests -- goes through `build_client()` /
`guarded_send()` here, not through httpx directly. This is the
platform's SSRF defense: validate the URL (scheme, no embedded
credentials), resolve the host and reject destinations that are
loopback, link-local (which covers cloud metadata endpoints -- every
major cloud's metadata service lives at a link-local address), multicast,
unspecified, or otherwise private, unless the host is explicitly
allowlisted (deployment-wide via settings, or per-tenant via
`apps.connections.models.AllowedOutboundHost`); disable redirects;
enforce bounded connect/read/write/pool timeouts; and cap response size
with a streamed, early-abort read rather than buffering an unbounded body.

Known residual risk, documented rather than hidden (see
docs/decisions/0006-outbound-http-security-policy.md): this resolves DNS
once, validates the resolved addresses, and then hands the *hostname*
(not a pinned IP) to httpx for the actual connection. A narrow
DNS-rebinding window between validation and connection -- the name
resolving to something different by the time httpx connects -- is not
closed by this pass. Closing it fully requires pinning the connection to
the validated IP (a custom transport), which is deliberately out of
scope here to keep this change reviewable; see the roadmap.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from apps.core.exceptions import OutboundRequestBlockedError
from apps.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 10.0
DEFAULT_POOL_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB

_ALLOWED_SCHEMES = {"http", "https"}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read=DEFAULT_READ_TIMEOUT_SECONDS,
        write=DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool=DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def build_client(**kwargs) -> httpx.Client:
    """An httpx.Client with the platform's bounded timeout policy and
    redirects disabled. Still validate each request's URL with
    `guarded_send()` (or `validate_outbound_url()` directly) -- building
    the client alone doesn't check destinations."""
    kwargs.setdefault("timeout", default_timeout())
    kwargs.setdefault("follow_redirects", False)
    return httpx.Client(**kwargs)


def _deployment_allowed_hosts() -> frozenset[str]:
    hosts = getattr(settings, "OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS", ())
    return frozenset(h.lower() for h in hosts)


def _tenant_allowed_hosts() -> frozenset[str]:
    try:
        from apps.connections.models import AllowedOutboundHost
    except ImportError:
        return frozenset()
    try:
        return frozenset(AllowedOutboundHost.objects.values_list("host", flat=True))
    except Exception:
        # No tenant schema active (public schema / no schema context) or
        # the table isn't migrated in this context -- fail SAFE, meaning
        # "no tenant allowlist", not "skip validation".
        log.debug("outbound_http.tenant_allowlist_unavailable")
        return frozenset()


def _is_host_allowlisted(host: str) -> bool:
    host = host.lower()
    return host in _deployment_allowed_hosts() or host in _tenant_allowed_hosts()


def _classify_blocked(ip: IPAddress) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (this range includes cloud metadata endpoints)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_private:
        return "private"
    return None


def resolve_host(host: str) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise OutboundRequestBlockedError(f"could not resolve host {host!r}: {exc}") from exc
    addresses: list[IPAddress] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    return addresses


def validate_outbound_url(url: str) -> None:
    """Raise `OutboundRequestBlockedError` if `url` may not be contacted
    under the current policy. Does not perform the request."""
    parts = urlsplit(url)

    if parts.scheme not in _ALLOWED_SCHEMES:
        raise OutboundRequestBlockedError(
            f"scheme {parts.scheme!r} is not allowed (only http/https)"
        )
    if parts.username or parts.password:
        raise OutboundRequestBlockedError("credentials embedded in a URL are not allowed")

    host = parts.hostname
    if not host:
        raise OutboundRequestBlockedError("URL has no host")

    allowlisted = _is_host_allowlisted(host)

    if parts.scheme == "http" and not settings.OUTBOUND_HTTP_ALLOW_INSECURE_HTTP:
        if not allowlisted:
            raise OutboundRequestBlockedError(
                "plain http:// is not allowed by default; use https:// or allowlist this host"
            )

    if allowlisted:
        return

    for ip in resolve_host(host):
        category = _classify_blocked(ip)
        if category is not None:
            raise OutboundRequestBlockedError(
                f"host {host!r} resolves to a {category} address; this destination is "
                "blocked unless explicitly allowlisted"
            )


def read_bounded_body(
    response: httpx.Response, *, max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
) -> bytes:
    """Read `response`'s body with a hard size cap, aborting (and closing
    the connection) as soon as the cap is exceeded rather than buffering
    an unbounded amount first. Requires the response to have been
    obtained with `stream=True`."""
    total = 0
    chunks: list[bytes] = []
    try:
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise OutboundRequestBlockedError(
                    f"response exceeded the maximum allowed size of {max_bytes} bytes"
                )
            chunks.append(chunk)
    finally:
        response.close()
    return b"".join(chunks)


def guarded_send(
    client: httpx.Client,
    request: httpx.Request,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    read_body: bool = True,
) -> tuple[httpx.Response, bytes]:
    """Validate `request.url`, send it with redirects disabled, refuse to
    follow any redirect response, and return `(response, body)` with
    `body` read under the size cap. Pass `read_body=False` when the
    caller only needs `response.status_code`/`.headers` (e.g. a
    connection test) -- the response is still closed without buffering
    anything."""
    validate_outbound_url(str(request.url))

    response = client.send(request, stream=True)

    if response.is_redirect:
        location = response.headers.get("location", "<unknown>")
        response.close()
        raise OutboundRequestBlockedError(
            f"redirect to {location!r} was not followed (redirects are disabled by policy)"
        )

    if not read_body:
        response.close()
        return response, b""

    body = read_bounded_body(response, max_bytes=max_response_bytes)
    return response, body
