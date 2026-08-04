import base64
from typing import Any

import httpx

from apps.authproviders.base import AuthProvider
from apps.authproviders.registry import register
from apps.core.exceptions import AuthenticationError


@register
class BasicAuthProvider(AuthProvider):
    """HTTP Basic authentication.

    Expected `credential` fields:
      - username (secret or non-secret, caller's choice)
      - password (secret)
    """

    type_key = "basic"

    def validate_credential_shape(self, credential: dict[str, Any]) -> None:
        if not credential.get("username") or not credential.get("password"):
            raise AuthenticationError("basic credential requires 'username' and 'password'")

    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        self.validate_credential_shape(credential)
        token = base64.b64encode(
            f"{credential['username']}:{credential['password']}".encode()
        ).decode()
        new_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.content,
        )
        new_request.headers["Authorization"] = f"Basic {token}"
        return new_request
