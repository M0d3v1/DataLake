import uuid

from django_tenants.test.cases import TenantTestCase

from apps.core.exceptions import SecretNotFound
from apps.secrets.backends.encrypted_field import EncryptedFieldSecretStore
from apps.secrets.models import EncryptedSecret


class EncryptedFieldSecretStoreTests(TenantTestCase):
    def test_round_trip(self):
        store = EncryptedFieldSecretStore()
        ref = store.store("super-secret-value")
        self.assertEqual(store.retrieve(ref), "super-secret-value")

    def test_stored_ciphertext_does_not_contain_plaintext(self):
        store = EncryptedFieldSecretStore()
        ref = store.store("another-secret")
        record = EncryptedSecret.objects.get(id=ref)
        self.assertNotIn(b"another-secret", bytes(record.ciphertext))

    def test_delete_removes_secret(self):
        store = EncryptedFieldSecretStore()
        ref = store.store("to-delete")
        store.delete(ref)
        with self.assertRaises(SecretNotFound):
            store.retrieve(ref)

    def test_retrieve_unknown_ref_raises_secret_not_found(self):
        store = EncryptedFieldSecretStore()
        with self.assertRaises(SecretNotFound):
            store.retrieve(str(uuid.uuid4()))
