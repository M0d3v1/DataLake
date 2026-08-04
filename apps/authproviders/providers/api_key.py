from typing import Any

import httpx

from apps.authproviders.base import AuthProvider
from apps.authproviders.registry import register
from apps.core.exceptions import AuthenticationError


@register
class ApiKeyAuthProvider(AuthProvider):
    """Sends a static API key as a request header.

    Expected `credential` fields:
      - api_key (secret): the key value.
      - header_name (optional, default "X-API-Key"): header to send it in.
    """

    type_key = "api_key"

    def validate_credential_shape(self, credential: dict[str, Any]) -> None:
        if not credential.get("api_key"):
            raise AuthenticationError("api_key credential requires an 'api_key' value")

    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        self.validate_credential_shape(credential)
        header_name = credential.get("header_name", "X-API-Key")
        new_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.content,
        )
        new_request.headers[header_name] = credential["api_key"]
        return new_request
