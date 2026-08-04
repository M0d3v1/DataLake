from typing import Any

import httpx

from apps.authproviders.base import AuthProvider
from apps.authproviders.registry import register
from apps.core.exceptions import AuthenticationError


@register
class BearerAuthProvider(AuthProvider):
    """Sends a static bearer token via the `Authorization` header.

    Expected `credential` fields:
      - token (secret): the bearer token value.
    """

    type_key = "bearer"

    def validate_credential_shape(self, credential: dict[str, Any]) -> None:
        if not credential.get("token"):
            raise AuthenticationError("bearer credential requires a 'token' value")

    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        self.validate_credential_shape(credential)
        new_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.content,
        )
        new_request.headers["Authorization"] = f"Bearer {credential['token']}"
        return new_request
