from typing import Any

import httpx

from apps.authproviders.registry import register
from apps.authproviders.token_support import (
    DEFAULT_TIMEOUT_SECONDS,
    CachedToken,
    TokenCachingAuthProvider,
    extract_expires_at,
    extract_value,
    validate_extract_config,
    validate_method,
    validate_timeout,
    validate_url,
)
from apps.core.exceptions import AuthenticationError


@register
class TokenEndpointAuthProvider(TokenCachingAuthProvider):
    """Exchanges credentials for a token at a single token endpoint (e.g.
    an OAuth2 client-credentials grant), caching it for reuse within one
    pipeline execution and re-acquiring on expiry or after `invalidate()`.

    Expected `credential` fields:
      - token_url (str, required): absolute http(s) URL.
      - method (str, default "POST")
      - request_body (dict, optional): JSON body for the token request
        (e.g. {"grant_type": "client_credentials", "client_id": "...",
        "client_secret": "..."}).
      - request_headers (dict, optional)
      - extract (dict, required): {"from": "json", "field": "access_token"}
        or {"from": "header", "header": "X-Access-Token"}.
      - expires_in_field (str, optional): dotted path to a
        seconds-until-expiry value in the token response, e.g. "expires_in".
      - inject_header (str, default "Authorization")
      - inject_prefix (str, default "Bearer ")
      - timeout_seconds (float, default 30, max 120)
    """

    type_key = "token_endpoint"

    def _acquire(self, credential: dict[str, Any]) -> CachedToken:
        url = validate_url(credential.get("token_url"), label="credential.token_url")
        method = validate_method(credential.get("method", "POST"), label="credential.method")
        timeout = validate_timeout(
            credential.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            label="credential.timeout_seconds",
        )
        extract = validate_extract_config(credential.get("extract"), label="credential.extract")

        headers = dict(credential.get("request_headers", {}))
        body = credential.get("request_body")
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if method == "POST" and body is not None:
            request_kwargs["json"] = body

        try:
            with httpx.Client() as client:
                response = client.request(method, url, **request_kwargs)
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"token endpoint request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AuthenticationError(f"token endpoint returned HTTP {response.status_code}")

        token_value = extract_value(response, extract)
        expires_at = extract_expires_at(response, credential.get("expires_in_field"))
        return CachedToken(value=token_value, expires_at=expires_at)
