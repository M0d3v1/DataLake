"""Secret storage abstraction.

Credential material (API keys, passwords, tokens) is never stored inline
on `Connection`/`Credential` rows. Instead, callers get an opaque
`secret_ref` back from `SecretStore.store()` and persist *that*. This
keeps the storage mechanism swappable -- the bundled
`EncryptedFieldSecretStore` (Fernet-encrypted rows in the tenant schema)
is a reasonable default for self-hosted/local use, but a production
deployment can implement this interface against Vault, AWS Secrets
Manager, etc. without touching any calling code. See
docs/decisions/0003-secret-store-abstraction.md.
"""

from abc import ABC, abstractmethod

from django.conf import settings
from django.utils.module_loading import import_string


class SecretStore(ABC):
    @abstractmethod
    def store(self, plaintext: str) -> str:
        """Persist `plaintext` and return an opaque reference to it.

        The reference is safe to store on a model field; it must not leak
        information about the plaintext value.
        """

    @abstractmethod
    def retrieve(self, secret_ref: str) -> str:
        """Return the plaintext previously stored under `secret_ref`.

        Raises `apps.core.exceptions.SecretNotFound` if the reference is
        unknown.
        """

    @abstractmethod
    def delete(self, secret_ref: str) -> None:
        """Irrecoverably remove the secret behind `secret_ref`, if present."""


_store_instance: SecretStore | None = None


def get_secret_store() -> SecretStore:
    """Return the configured SecretStore singleton (see
    settings.SECRET_STORE_BACKEND)."""
    global _store_instance
    if _store_instance is None:
        backend_cls = import_string(settings.SECRET_STORE_BACKEND)
        _store_instance = backend_cls()
    return _store_instance
