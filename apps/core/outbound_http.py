"""Shared outbound HTTP security policy.

Every outbound call this platform makes to a tenant-configured URL --
REST source requests, token-endpoint and multi-step authentication
requests, and connection tests -- goes through `build_client()` /
`guarded_send()` here, not through httpx directly. This is the
platform's SSRF defense: validate the URL (scheme, no embedded
credentials), resolve the host and reject destinations that are
loopback, link-local (which covers cloud metadata endpoints -- every
major cloud's metadata service lives at a link-local address), multicast,
unspecified, or otherwise private; disable redirects; enforce bounded
connect/read/write/pool timeouts; and cap response size with a streamed,
early-abort read rather than buffering an unbounded body.

**Allowlisting is deliberately layered, not one flag:**

- `settings.OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS` (deployment-wide) and
  `apps.connections.models.AllowedOutboundHost` (per-tenant) both bypass
  only the "private" (RFC1918/ULA/documentation-range) category -- the
  legitimate "internal enterprise system" case. Neither ever bypasses
  loopback, link-local (cloud metadata), multicast, or unspecified: a
  tenant-configured allowlist entry must never be able to reach the
  platform's own loopback interface or a cloud metadata endpoint.
- `settings.OUTBOUND_HTTP_ALLOWED_UNSAFE_HOSTS` (deployment-only, no
  tenant equivalent) is the sole way to bypass those higher-risk
  categories, for the rare deployment that genuinely needs it (e.g. a
  local sidecar reachable at 127.0.0.1 in an isolated environment).
- Being on either host-address allowlist above does **not** implicitly
  permit plain `http://` -- that's a separate permission
  (`settings.OUTBOUND_HTTP_ALLOW_INSECURE_HTTP` deployment-wide, or
  `settings.OUTBOUND_HTTP_INSECURE_ALLOWED_HOSTS` per-host), since
  approving a destination's *address* says nothing about whether it's
  safe to reach unencrypted.

See docs/decisions/0006-outbound-http-security-policy.md.

Known residual risk, documented rather than hidden: this resolves DNS
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
DEFAULT_MAX_TIMEOUT_SECONDS = 120.0

_ALLOWED_SCHEMES = {"http", "https"}

# Categories a plain deployment/tenant "private hosts" allowlist entry is
# allowed to bypass. Everything else (loopback, link-local, multicast,
# unspecified) requires the separate, deployment-only "unsafe hosts"
# allowlist -- see the module docstring.
_PRIVATE_ALLOWLISTABLE_CATEGORY = "private"

_BLOCKED_CATEGORY_DESCRIPTIONS = {
    "loopback": "loopback",
    "link-local": "link-local (this range includes cloud metadata endpoints)",
    "multicast": "multicast",
    "unspecified": "unspecified",
    "private": "private",
}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read=DEFAULT_READ_TIMEOUT_SECONDS,
        write=DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool=DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def resolve_max_response_bytes(requested: int | None = None) -> int:
    """Apply the deployment's hard ceiling to a connector-requested
    `max_response_bytes`. A connector config may lower the effective cap,
    never raise it past `settings.OUTBOUND_HTTP_MAX_RESPONSE_BYTES` --
    otherwise a single connection's config would silently override a
    deployment-wide memory/bandwidth safety limit."""
    ceiling = getattr(settings, "OUTBOUND_HTTP_MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES)
    if requested is None:
        return ceiling
    return min(requested, ceiling)


def resolve_timeout_seconds(requested: float | None = None) -> float:
    """Apply the deployment's hard ceiling to a connector-requested
    timeout (any single phase: connect/read/write/pool). A connector
    config may lower it, never raise it past
    `settings.OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS`."""
    ceiling = getattr(settings, "OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS", DEFAULT_MAX_TIMEOUT_SECONDS)
    if requested is None:
        return ceiling
    return min(requested, ceiling)


def build_client(**kwargs) -> httpx.Client:
    """An httpx.Client with the platform's bounded timeout policy,
    redirects disabled, and no implicit environment-driven proxy/netrc
    pickup (`trust_env=False`) -- a proxy is only ever used if a caller
    explicitly passes one via `proxy=`/`mounts=`, a deployment-controlled
    decision, never one silently inherited from `HTTP_PROXY`/`.netrc` in
    the worker process's environment. Still validate each request's URL
    with `guarded_send()` (or `validate_outbound_url()` directly) --
    building the client alone doesn't check destinations."""
    kwargs.setdefault("timeout", default_timeout())
    kwargs.setdefault("follow_redirects", False)
    kwargs.setdefault("trust_env", False)
    return httpx.Client(**kwargs)


def _deployment_allowed_private_hosts() -> frozenset[str]:
    hosts = getattr(settings, "OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS", ())
    return frozenset(h.lower() for h in hosts)


def _deployment_allowed_unsafe_hosts() -> frozenset[str]:
    hosts = getattr(settings, "OUTBOUND_HTTP_ALLOWED_UNSAFE_HOSTS", ())
    return frozenset(h.lower() for h in hosts)


def _deployment_insecure_http_hosts() -> frozenset[str]:
    hosts = getattr(settings, "OUTBOUND_HTTP_INSECURE_ALLOWED_HOSTS", ())
    return frozenset(h.lower() for h in hosts)


def _tenant_allowed_private_hosts() -> frozenset[str]:
    try:
        from apps.connections.models import AllowedOutboundHost
    except ImportError:
        return frozenset()
    try:
        return frozenset(
            h.lower() for h in AllowedOutboundHost.objects.values_list("host", flat=True)
        )
    except Exception:
        # No tenant schema active (public schema / no schema context) or
        # the table isn't migrated in this context -- fail SAFE, meaning
        # "no tenant allowlist", not "skip validation".
        log.debug("outbound_http.tenant_allowlist_unavailable")
        return frozenset()


def _is_private_allowlisted(host: str) -> bool:
    host = host.lower()
    return host in _deployment_allowed_private_hosts() or host in _tenant_allowed_private_hosts()


def _is_unsafe_allowlisted(host: str) -> bool:
    return host.lower() in _deployment_allowed_unsafe_hosts()


def _is_http_scheme_allowed(host: str) -> bool:
    if getattr(settings, "OUTBOUND_HTTP_ALLOW_INSECURE_HTTP", False):
        return True
    return host.lower() in _deployment_insecure_http_hosts()


def _classify_blocked(ip: IPAddress) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
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

    if parts.scheme == "http" and not _is_http_scheme_allowed(host):
        raise OutboundRequestBlockedError(
            "plain http:// is not allowed by default; use https://, or enable it explicitly "
            "via OUTBOUND_HTTP_ALLOW_INSECURE_HTTP / OUTBOUND_HTTP_INSECURE_ALLOWED_HOSTS -- "
            "being on the destination allowlist does not by itself permit http://"
        )

    private_allowlisted = _is_private_allowlisted(host)
    unsafe_allowlisted = _is_unsafe_allowlisted(host)

    for ip in resolve_host(host):
        category = _classify_blocked(ip)
        if category is None:
            continue
        if category == _PRIVATE_ALLOWLISTABLE_CATEGORY and private_allowlisted:
            continue
        if unsafe_allowlisted:
            continue
        raise OutboundRequestBlockedError(
            f"host {host!r} resolves to a {_BLOCKED_CATEGORY_DESCRIPTIONS[category]} address; "
            "this destination is blocked unless explicitly allowlisted"
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


def _sanitize_redirect_location(location: str) -> str:
    """Reduce a redirect `Location` header to scheme+host+port+path only,
    for safe inclusion in an error message or log line. Strips query
    strings, fragments, and any embedded credentials -- a redirect target
    is tenant/attacker-controlled response data and routinely carries
    signed URLs, session tokens, or other sensitive values in its query
    string that must never be persisted onto `PipelineRun.error_message`
    or a log event. If the header doesn't parse as a URL with a host, it
    is omitted entirely rather than echoed as-is."""
    try:
        parts = urlsplit(location)
    except ValueError:
        return "<redirect-target-omitted>"
    if not parts.hostname:
        return "<redirect-target-omitted>"
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return f"{parts.scheme}://{netloc}{parts.path or ''}" if parts.scheme else netloc


def guarded_send(
    client: httpx.Client,
    request: httpx.Request,
    *,
    max_response_bytes: int | None = None,
    read_body: bool = True,
) -> tuple[httpx.Response, bytes]:
    """Validate `request.url`, send it with redirects disabled, refuse to
    follow any redirect response, and return `(response, body)` with
    `body` read under the size cap. `max_response_bytes` is a connector's
    *requested* cap, if any -- it is always clamped to the deployment's
    hard ceiling (`resolve_max_response_bytes`), so a connection's own
    configuration can lower the effective limit but never raise it past
    what the deployment allows. Pass `read_body=False` when the caller
    only needs `response.status_code`/`.headers` (e.g. a connection
    test) -- the response is still closed without buffering anything."""
    validate_outbound_url(str(request.url))
    effective_max_bytes = resolve_max_response_bytes(max_response_bytes)

    response = client.send(request, stream=True)

    if response.is_redirect:
        location = _sanitize_redirect_location(response.headers.get("location", ""))
        response.close()
        raise OutboundRequestBlockedError(
            f"redirect to {location!r} was not followed (redirects are disabled by policy)"
        )

    if not read_body:
        response.close()
        return response, b""

    body = read_bounded_body(response, max_bytes=effective_max_bytes)
    return response, body
