import uuid

from django.db import models

from apps.core.models import TimeStampedModel


class EncryptedSecret(TimeStampedModel):
    """Ciphertext storage for the default `EncryptedFieldSecretStore`.

    Tenant-scoped: each organization's secrets live only in that
    organization's Postgres schema. Never queried or exposed directly by
    application code -- always go through `SecretStore`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ciphertext = models.BinaryField()

    def __str__(self) -> str:
        return f"EncryptedSecret({self.id})"
