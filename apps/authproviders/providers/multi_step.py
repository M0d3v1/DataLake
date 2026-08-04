from typing import Any

import httpx

from apps.authproviders.base import AuthProvider
from apps.authproviders.registry import register


@register
class MultiStepTokenAuthProvider(AuthProvider):
    """Multi-step token acquisition (e.g. obtain -> exchange -> refresh),
    modeled as an explicit, declarative sequence of HTTP steps rather than
    arbitrary user code -- each step is a described request/response
    mapping, not a script.

    Interface reserved for Milestone 2; see TokenEndpointAuthProvider for
    the same reservation pattern.
    """

    type_key = "multi_step_token"

    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        raise NotImplementedError(
            "MultiStepTokenAuthProvider is not implemented yet (planned for Milestone 2)"
        )
