from django_tenants.test.cases import TenantTestCase

from apps.auditing.service import record_audit_event
from apps.connections.models import Connection


class RecordAuditEventTests(TenantTestCase):
    def test_record_audit_event_captures_target_and_metadata(self):
        connection = Connection.objects.create(
            name="Policies API", kind=Connection.Kind.SOURCE, connector_type="rest_api", config={}
        )

        log = record_audit_event(
            actor=None,
            action="connection.created",
            target=connection,
            metadata={"connector_type": "rest_api"},
        )

        self.assertEqual(log.action, "connection.created")
        self.assertEqual(log.target_type, "Connection")
        self.assertEqual(log.target_id, str(connection.pk))
        self.assertEqual(log.metadata, {"connector_type": "rest_api"})
        self.assertIsNone(log.actor)
