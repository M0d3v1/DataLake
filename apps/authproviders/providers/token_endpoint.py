from typing import Any

import httpx

from apps.authproviders.base import AuthProvider
from apps.authproviders.registry import register


@register
class TokenEndpointAuthProvider(AuthProvider):
    """Exchanges credentials for a short-lived token at a token endpoint
    (e.g. OAuth2 client-credentials grant), caching and refreshing it.

    Interface reserved for Milestone 2. Registered now so the connection
    UI/API can already offer "token endpoint" as an auth type and so this
    is a visible extension point -- `prepare_request` intentionally raises
    until the token cache/refresh logic lands.
    """

    type_key = "token_endpoint"

    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        raise NotImplementedError(
            "TokenEndpointAuthProvider is not implemented yet (planned for Milestone 2)"
        )
