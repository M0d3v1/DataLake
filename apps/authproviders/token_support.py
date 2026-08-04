"""Shared support for token-acquiring AuthProviders (`token_endpoint`,
`multi_step_token`): a bounded, declarative way to call one or more HTTP
endpoints, pull a token out of a JSON field or a response header, cache
it for the lifetime of one pipeline execution, and inject it into later
requests.

Deliberately not a template engine or a scripting hook: placeholder
substitution only recognizes `{{credential.<path>}}` and
`{{steps.<path>}}` via a whitelisted regex + dict lookup -- no `eval`, no
`exec`, no arbitrary expressions. An unresolved placeholder is a
configuration error, not a silently-ignored literal string sent to a real
endpoint.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings

from apps.authproviders.base import AuthProvider
from apps.core.exceptions import AuthenticationError, ConfigurationError
from apps.core.outbound_http import DEFAULT_MAX_TIMEOUT_SECONDS
from apps.core.paths import get_by_path

DEFAULT_TIMEOUT_SECONDS = 30
MAX_MULTI_STEPS = 5
_ALLOWED_METHODS = {"GET", "POST"}
_EXPIRY_SAFETY_MARGIN_SECONDS = 30

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(credential|steps)\.([A-Za-z0-9_.]+)\s*\}\}")


@dataclass
class CachedToken:
    value: str
    # Absolute `time.monotonic()` deadline, or None if the token never
    # expires (or no expiry information was available).
    expires_at: float | None


def is_expired(token: CachedToken | None) -> bool:
    if token is None:
        return True
    if token.expires_at is None:
        return False
    return time.monotonic() >= token.expires_at


# --- config validation ------------------------------------------------
# Every raise here is a ConfigurationError: bad auth config isn't fixed
# by retrying, it's fixed by the tenant admin correcting the connection.


def validate_url(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not (
        value.startswith("http://") or value.startswith("https://")
    ):
        raise ConfigurationError(f"{label} must be an absolute http:// or https:// URL")
    return value


def validate_method(value: Any, *, label: str) -> str:
    method = str(value).upper() if value else "POST"
    if method not in _ALLOWED_METHODS:
        raise ConfigurationError(
            f"{label} must be one of {sorted(_ALLOWED_METHODS)}, got {value!r}"
        )
    return method


def validate_header_name(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or ("\r" in value or "\n" in value):
        raise ConfigurationError(f"{label} must be a non-empty header name")
    return value


def validate_field_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty field path")
    return value


def validate_timeout(value: Any, *, label: str) -> float:
    # Same deployment-wide ceiling apps.core.outbound_http applies to the
    # REST source connector -- a connection's config may request a lower
    # timeout, never one exceeding what the deployment allows for any
    # outbound call, authentication included.
    max_timeout_seconds = getattr(
        settings, "OUTBOUND_HTTP_MAX_TIMEOUT_SECONDS", DEFAULT_MAX_TIMEOUT_SECONDS
    )
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"{label} must be a positive number")
    if value > max_timeout_seconds:
        raise ConfigurationError(f"{label} must not exceed {max_timeout_seconds} seconds")
    return float(value)


def validate_extract_config(extract: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(extract, dict):
        raise ConfigurationError(f"{label} must be an object")
    source = extract.get("from")
    if source not in ("json", "header"):
        raise ConfigurationError(f"{label}.from must be 'json' or 'header'")
    if source == "json":
        validate_field_path(extract.get("field"), label=f"{label}.field")
    else:
        validate_header_name(extract.get("header"), label=f"{label}.header")
    return extract


# --- placeholder substitution -------------------------------------------


def substitute_placeholders(value: Any, context: dict[str, dict[str, Any]]) -> Any:
    """Recursively substitute `{{credential.x}}` / `{{steps.y}}`
    placeholders inside strings, dicts, and lists. Non-string, non-dict,
    non-list values pass through unchanged."""
    if isinstance(value, str):

        def _replace(match: re.Match) -> str:
            namespace, path = match.group(1), match.group(2)
            resolved, found = get_by_path(context.get(namespace, {}), path)
            if not found:
                raise ConfigurationError(f"unresolved placeholder {{{{{namespace}.{path}}}}}")
            return str(resolved)

        return _PLACEHOLDER_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {key: substitute_placeholders(val, context) for key, val in value.items()}
    if isinstance(value, list):
        return [substitute_placeholders(val, context) for val in value]
    return value


# --- extraction -----------------------------------------------------------


def extract_value(response: httpx.Response, body: bytes, extract: dict[str, Any]) -> str:
    if extract["from"] == "header":
        header_name = extract["header"]
        value = response.headers.get(header_name)
        if value is None:
            raise AuthenticationError(f"authentication response missing header {header_name!r}")
        return value

    try:
        data = json.loads(body)
    except ValueError as exc:
        raise AuthenticationError("authentication response was not valid JSON") from exc
    field = extract["field"]
    value, found = get_by_path(data, field)
    if not found:
        raise AuthenticationError(f"authentication response missing field {field!r}")
    return str(value)


def extract_expires_at(body: bytes, expires_in_field: str | None) -> float | None:
    """Best-effort: a token with no readable expiry is treated as
    long-lived (cached until `invalidate()` is called), not an error --
    expiry is an optional refinement, not something every token endpoint
    is required to provide."""
    if not expires_in_field:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    value, found = get_by_path(data, expires_in_field)
    if not found:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return time.monotonic() + max(seconds - _EXPIRY_SAFETY_MARGIN_SECONDS, 0)


# --- shared provider base --------------------------------------------------


class TokenCachingAuthProvider(AuthProvider):
    """Shared cache/inject/invalidate logic for providers that acquire a
    token via one or more HTTP calls. One provider *instance* is created
    per `SourceConnector.fetch()` call (see
    `RestApiSourceConnector._resolve_auth_provider`), so the cache here
    lives for exactly one pipeline execution's extract phase -- not
    longer, not shared across runs. Subclasses implement `_acquire`.
    """

    def __init__(self) -> None:
        self._cache: CachedToken | None = None

    def _acquire(self, credential: dict[str, Any]) -> CachedToken:
        raise NotImplementedError

    def invalidate(self, credential: dict[str, Any]) -> None:
        self._cache = None

    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        if is_expired(self._cache):
            self._cache = self._acquire(credential)

        header = validate_header_name(
            credential.get("inject_header", "Authorization"), label="credential.inject_header"
        )
        prefix = credential.get("inject_prefix", "Bearer ")

        new_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.content,
        )
        new_request.headers[header] = f"{prefix}{self._cache.value}"
        return new_request
