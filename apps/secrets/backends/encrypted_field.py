from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from apps.core.exceptions import SecretNotFound
from apps.secrets.base import SecretStore
from apps.secrets.models import EncryptedSecret


class EncryptedFieldSecretStore(SecretStore):
    """Default SecretStore: Fernet-encrypted ciphertext in a tenant-scoped
    Postgres table, keyed by a random UUID.

    This is intentionally simple and has real limitations: the encryption
    key is a single application-level secret (`settings.SECRET_STORE_ENCRYPTION_KEY`)
    with no rotation or hardware-backed protection. It's suitable for local
    development and small self-hosted deployments; production deployments
    handling real customer credentials should provide a `SecretStore`
    implementation backed by a managed secrets service instead.
    """

    def __init__(self) -> None:
        self._fernet = Fernet(settings.SECRET_STORE_ENCRYPTION_KEY.encode())

    def store(self, plaintext: str) -> str:
        ciphertext = self._fernet.encrypt(plaintext.encode())
        record = EncryptedSecret.objects.create(ciphertext=ciphertext)
        return str(record.id)

    def retrieve(self, secret_ref: str) -> str:
        try:
            record = EncryptedSecret.objects.get(id=secret_ref)
        except EncryptedSecret.DoesNotExist as exc:
            raise SecretNotFound(f"No secret found for ref {secret_ref!r}") from exc
        try:
            return self._fernet.decrypt(bytes(record.ciphertext)).decode()
        except InvalidToken as exc:
            raise SecretNotFound(
                f"Secret {secret_ref!r} could not be decrypted (wrong key?)"
            ) from exc

    def delete(self, secret_ref: str) -> None:
        EncryptedSecret.objects.filter(id=secret_ref).delete()
