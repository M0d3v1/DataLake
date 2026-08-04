"""Authentication-provider interface.

Authentication for source/destination connections is expressed as a
fixed, extensible set of provider *classes* -- not user-supplied code.
Each provider implements a narrow contract (take an outgoing httpx
request and a decrypted credential payload, return an authenticated
request). Adding a new auth style means adding a new provider class and
registering it, which keeps the platform's attack surface reviewable
instead of accepting arbitrary logic from end users.

`credential` dicts contain whatever secret + non-secret fields that
provider needs (e.g. ApiKeyAuthProvider expects `api_key` and
`header_name`); the calling code is responsible for resolving any
`secret_ref` via `apps.secrets` before invoking a provider.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx


class AuthProvider(ABC):
    #: Short, stable identifier stored on `Credential.auth_provider_type`
    #: and used as the registry key (e.g. "api_key", "bearer").
    type_key: ClassVar[str]

    @abstractmethod
    def prepare_request(self, request: httpx.Request, credential: dict[str, Any]) -> httpx.Request:
        """Return a new request with authentication applied.

        Implementations must not mutate `request` in place -- callers may
        reuse the original for retries with a freshly resolved credential
        (e.g. after a token refresh).
        """

    def validate_credential_shape(self, credential: dict[str, Any]) -> None:  # noqa: B027
        """Raise `apps.core.exceptions.AuthenticationError` if `credential`
        is missing fields this provider requires. Default: no-op; concrete
        providers override to fail fast with a clear message instead of a
        KeyError deep inside `prepare_request`."""

    def invalidate(self, credential: dict[str, Any]) -> None:  # noqa: B027
        """Discard any cached token/state for `credential`, forcing the
        next `prepare_request` to re-acquire. Default: no-op (stateless
        providers like api_key/basic/bearer have nothing to invalidate).
        Token-based providers override this; callers use it after a
        source responds 401/403 despite a request that looked
        authenticated, to force one re-auth-and-retry rather than looping
        forever on a stale cached token."""
