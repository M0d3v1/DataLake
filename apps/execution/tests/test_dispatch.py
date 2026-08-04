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

    # --- continuation (item 1: resuming after PageLimitExceededError) -----

    def _make_page_limit_failed_run(self, pipeline: Pipeline) -> PipelineRun:
        return PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key=f"failed-{pipeline.id}",
            status=PipelineRun.Status.FAILED,
            error_category="PageLimitExceededError",
            error_is_retryable=False,
            last_successful_cursor="4",
            pages_extracted=3,
            records_extracted=3,
            records_loaded=3,
            raw_payload_count=3,
        )

    def test_continue_from_seeds_cursor_and_counters_on_the_new_run(self):
        pipeline = self._make_pipeline()
        failed_run = self._make_page_limit_failed_run(pipeline)

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            run, dispatched = trigger_manual_run(
                pipeline, schema_name=self.tenant.schema_name, continue_from=failed_run
            )

        self.assertTrue(dispatched)
        self.assertNotEqual(run.id, failed_run.id)
        self.assertEqual(run.continued_from_id, failed_run.id)
        self.assertEqual(run.last_successful_cursor, "4")
        self.assertEqual(run.pages_extracted, 3)
        self.assertEqual(run.records_extracted, 3)
        self.assertEqual(run.records_loaded, 3)
        self.assertEqual(run.raw_payload_count, 3)
        mock_task.delay.assert_called_once_with(
            schema_name=self.tenant.schema_name, run_id=str(run.id)
        )

    def test_a_plain_fresh_trigger_does_not_carry_over_cursor_state(self):
        # Regression guard for the exact bug item 1 fixes: without an
        # explicit continue_from, a new run must start clean, not resume.
        pipeline = self._make_pipeline()
        self._make_page_limit_failed_run(pipeline)

        with patch("apps.execution.dispatch.run_pipeline_task"):
            run, _ = trigger_manual_run(pipeline, schema_name=self.tenant.schema_name)

        self.assertIsNone(run.continued_from_id)
        self.assertIsNone(run.last_successful_cursor)
        self.assertEqual(run.pages_extracted, 0)

    def test_continue_from_rejects_a_run_that_did_not_fail_with_page_limit_exceeded(self):
        pipeline = self._make_pipeline()
        run_with_other_failure = PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key="other-failure",
            status=PipelineRun.Status.FAILED,
            error_category="FetchFailed",
        )

        with self.assertRaises(ConfigurationError):
            trigger_manual_run(
                pipeline,
                schema_name=self.tenant.schema_name,
                continue_from=run_with_other_failure,
            )

    def test_continue_from_rejects_a_run_that_is_not_terminal(self):
        pipeline = self._make_pipeline()
        running_run = PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key="still-running",
            status=PipelineRun.Status.RUNNING,
        )

        with self.assertRaises(ConfigurationError):
            trigger_manual_run(
                pipeline, schema_name=self.tenant.schema_name, continue_from=running_run
            )

    def test_continue_from_rejects_a_run_belonging_to_a_different_pipeline(self):
        pipeline_a = self._make_pipeline(name="Pipeline A")
        pipeline_b = self._make_pipeline(name="Pipeline B")
        failed_run = self._make_page_limit_failed_run(pipeline_a)

        with self.assertRaises(ConfigurationError):
            trigger_manual_run(
                pipeline_b, schema_name=self.tenant.schema_name, continue_from=failed_run
            )
