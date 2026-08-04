from unittest.mock import patch

from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.core.exceptions import ConfigurationError
from apps.execution.dispatch import trigger_manual_run
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline


class TriggerManualRunTests(TenantTestCase):
    def _make_pipeline(self, **overrides) -> Pipeline:
        suffix = Connection.objects.count()
        source = Connection.objects.create(
            name=f"Policies API {suffix}",
            kind=Connection.Kind.SOURCE,
            connector_type="rest_api",
            config={},
        )
        destination = Connection.objects.create(
            name=f"Warehouse {suffix}",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={},
        )
        defaults = {
            "name": "Policies sync",
            "source_connection": source,
            "destination_connection": destination,
            "destination_mapping": {"id": "id"},
        }
        defaults.update(overrides)
        return Pipeline.objects.create(**defaults)

    def test_dispatches_a_new_run_and_enqueues_the_task(self):
        pipeline = self._make_pipeline()

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            run, dispatched = trigger_manual_run(pipeline, schema_name=self.tenant.schema_name)

        self.assertTrue(dispatched)
        self.assertEqual(run.pipeline_id, pipeline.id)
        self.assertEqual(run.trigger, PipelineRun.Trigger.MANUAL)
        mock_task.delay.assert_called_once_with(
            schema_name=self.tenant.schema_name, run_id=str(run.id)
        )

    def test_inactive_pipeline_is_rejected(self):
        pipeline = self._make_pipeline(is_active=False)

        with self.assertRaises(ConfigurationError):
            trigger_manual_run(pipeline, schema_name=self.tenant.schema_name)

    def test_duplicate_dispatch_with_same_idempotency_key_does_not_enqueue_twice(self):
        pipeline = self._make_pipeline()

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            run1, dispatched1 = trigger_manual_run(
                pipeline, schema_name=self.tenant.schema_name, idempotency_key="daily-sync"
            )
            run1.status = PipelineRun.Status.RUNNING
            run1.save(update_fields=["status"])

            run2, dispatched2 = trigger_manual_run(
                pipeline, schema_name=self.tenant.schema_name, idempotency_key="daily-sync"
            )

        self.assertTrue(dispatched1)
        self.assertFalse(dispatched2)
        self.assertEqual(run1.id, run2.id)
        self.assertEqual(PipelineRun.objects.filter(idempotency_key="daily-sync").count(), 1)
        mock_task.delay.assert_called_once()

    def test_reusing_key_of_a_pending_run_redispatches(self):
        pipeline = self._make_pipeline()

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            run1, dispatched1 = trigger_manual_run(
                pipeline, schema_name=self.tenant.schema_name, idempotency_key="stuck-pending"
            )
            # run1 stays PENDING (simulating a dispatch whose .delay() never
            # reached the broker)
            run2, dispatched2 = trigger_manual_run(
                pipeline, schema_name=self.tenant.schema_name, idempotency_key="stuck-pending"
            )

        self.assertTrue(dispatched1)
        self.assertTrue(dispatched2)
        self.assertEqual(run1.id, run2.id)
        self.assertEqual(mock_task.delay.call_count, 2)

    def test_no_explicit_key_creates_a_fresh_run_each_call(self):
        pipeline = self._make_pipeline()

        with patch("apps.execution.dispatch.run_pipeline_task"):
            run1, _ = trigger_manual_run(pipeline, schema_name=self.tenant.schema_name)
            run2, _ = trigger_manual_run(pipeline, schema_name=self.tenant.schema_name)

        self.assertNotEqual(run1.id, run2.id)

    def test_reused_key_for_a_different_pipeline_is_rejected(self):
        pipeline_a = self._make_pipeline(name="Pipeline A")
        pipeline_b = self._make_pipeline(name="Pipeline B")

        with patch("apps.execution.dispatch.run_pipeline_task"):
            trigger_manual_run(
                pipeline_a, schema_name=self.tenant.schema_name, idempotency_key="shared-key"
            )
            with self.assertRaises(ConfigurationError):
                trigger_manual_run(
                    pipeline_b, schema_name=self.tenant.schema_name, idempotency_key="shared-key"
                )
