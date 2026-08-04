# 0006: Shared outbound HTTP security policy (SSRF hardening)

## Status

Accepted (correctness/security hardening pass).

## Context

REST source URLs, token-endpoint URLs, and multi-step authentication step
URLs are all tenant-configured strings. A worker process resolves and
connects to whatever the configuration says. Without a shared policy,
each connector/provider would need to reimplement (or, more likely,
forget to implement) protection against a well-known class of attack:
Server-Side Request Forgery (SSRF), where a configured "external API"
actually points the worker at internal infrastructure -- the platform's
own metadata database credentials service, another tenant's service on
the same private network, a cloud provider's instance metadata endpoint,
or a redirect chain that ends up somewhere never intended.

Before this change, `RestApiSourceConnector`, `TokenEndpointAuthProvider`,
and `MultiStepTokenAuthProvider` each built their own bare `httpx.Client()`
with no destination validation, no response size limit, and (for the
auth providers) no explicit redirect handling.

## Decision

One module, `apps.core.outbound_http`, is now the *only* way any of these
three call sites reach the network. It provides `build_client()` (a
pre-configured `httpx.Client`: bounded connect/read/write/pool timeouts,
redirects disabled) and `guarded_send()` (validates the destination,
sends, refuses to follow a redirect response, reads the body under a hard
size cap with early abort rather than full buffering).

**Validation, in order:**
1. Scheme must be `http` or `https`. Plain `http://` is refused unless
   `settings.OUTBOUND_HTTP_ALLOW_INSECURE_HTTP` is set (deployment-wide)
   *or* the host is allowlisted -- HTTPS is the default expectation.
2. No credentials embedded in the URL (`user:pass@host`).
3. If the host is allowlisted (deployment-wide via
   `settings.OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS`, or per-tenant via
   `apps.connections.models.AllowedOutboundHost`), the destination checks
   below are skipped entirely -- allowlisting is how "explicitly approved
   private enterprise hosts" are supported.
4. Otherwise, the host is resolved (`socket.getaddrinfo`) and *every*
   resolved address is checked; if any is loopback, link-local
   (deliberately not special-cased further -- this range is exactly
   where every major cloud's instance metadata service lives, so
   blocking link-local blocks cloud metadata SSRF for free), multicast,
   unspecified, or otherwise private (RFC1918/ULA/documentation ranges),
   the request is refused.

A blocked request raises `OutboundRequestBlockedError` -- always
non-retryable, since the same configuration produces the same policy
decision every time; fixing it requires either correcting the
configuration or adding an allowlist entry, not a retry.

**Response handling:** `guarded_send()` sends with `stream=True` and
never calls `response.content`/`.json()` directly. If the response is a
redirect (3xx with `Location`), it's refused outright rather than
followed -- this is the "disable redirects" choice from the two options
offered (disable vs. validate-every-target); disabling is simpler,
strictly safer, and matches httpx's own default behavior for a bare
client. The body is then read via `read_bounded_body()`, which iterates
`response.iter_bytes()` and raises the moment the cumulative size passes
`max_response_bytes` (default 10 MiB, overridable per-connector config),
so an oversized or slow-drip response is aborted early rather than fully
buffered into memory first.

**Wiring:** `RestApiSourceConnector` (`_client`/`_send`), and both
`TokenEndpointAuthProvider` and `MultiStepTokenAuthProvider` (`_acquire`)
now build their client via `build_client()` and send via `guarded_send()`.
Because `test_connection()` shares `_send_with_auth_retry()`/`_send()`
with `fetch()` in the REST connector, connection tests get the same
protection automatically, not as a separate implementation.

## Known limitation (documented, not hidden)

This resolves DNS **once**, validates the resolved addresses, and then
hands the **hostname** (not a pinned IP) to httpx for the actual
connection. A narrow DNS-rebinding window exists: if the name resolves to
something different by the time httpx's own connection attempt happens
milliseconds later, the validated address and the connected address can
differ. Closing this fully requires pinning the connection to the
validated IP (e.g. a custom httpx transport that connects by IP while
still presenting the original hostname for TLS SNI/certificate
validation) -- a meaningfully larger, separately-reviewable change. This
pass closes the overwhelmingly common case (a configured URL that is
simply, statically, an internal address) and documents the narrower
rebinding case as the next hardening task rather than silently leaving it
unmentioned.

Also out of scope here: allowlist entries are matched by exact hostname
string, not by IP/CIDR. An operator approving `internal.example.test`
does not thereby approve whatever IP that hostname happens to resolve to
under a different name -- this is intentionally conservative (favors
false rejections needing an allowlist update over false approvals).

## Consequences

- REST source configuration, token-endpoint configuration, and
  multi-step authentication configuration can no longer be used to reach
  the platform's own internal network, another tenant's internal
  systems, or a cloud metadata endpoint, without an explicit,
  auditable allowlist entry.
- Legitimate internal-enterprise integrations (the product's own stated
  use case -- insurers integrating with partner and internal systems)
  remain possible via `AllowedOutboundHost`, scoped per tenant, or via
  the deployment-wide setting for infrastructure every tenant on a
  deployment may reach.
- Test suite impact: ~100 existing tests use fictional, non-resolvable
  hostnames (the `*.example-insurer.test` convention). A single root
  `conftest.py` autouse fixture fakes DNS resolution to a fixed public
  address so none of them needed individual changes; the policy's own
  test suite (`apps/core/tests/test_outbound_http.py`) overrides that
  fixture per-test to exercise the real blocking behavior.
