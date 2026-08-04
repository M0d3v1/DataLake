from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection, Credential


class CredentialTests(TenantTestCase):
    def test_credential_round_trips_through_secret_store(self):
        connection = Connection.objects.create(
            name="Policies API",
            kind=Connection.Kind.SOURCE,
            connector_type="rest_api",
            config={"base_url": "https://api.example-insurer.test"},
        )

        credential = Credential.create_for_connection(connection, "bearer", {"token": "tok_abc"})

        self.assertEqual(credential.resolve(), {"token": "tok_abc"})
        self.assertEqual(credential.auth_provider_type, "bearer")
        self.assertEqual(connection.credential, credential)
