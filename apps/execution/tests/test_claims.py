from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.execution.claims import (
    InvalidRunTransition,
    RunClaimRejected,
    claim_run,
    mark_run_failed,
    mark_run_succeeded,
)
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline


class ClaimsTests(TenantTestCase):
    def _make_run(self, **overrides) -> PipelineRun:
        source = Connection.objects.create(
            name="Policies API", kind=Connection.Kind.SOURCE, connector_type="rest_api", config={}
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={},
        )
        pipeline = Pipeline.objects.create(
            name="Policies sync", source_connection=source, destination_connection=destination
        )
        defaults = {"pipeline": pipeline, "idempotency_key": f"key-{Connection.objects.count()}"}
        defaults.update(overrides)
        return PipelineRun.objects.create(**defaults)

    def test_claim_pending_run_transitions_to_running(self):
        run = self._make_run()

        claimed = claim_run(str(run.id), "task-1")

        self.assertEqual(claimed.status, PipelineRun.Status.RUNNING)
        self.assertEqual(claimed.celery_task_id, "task-1")
        self.assertEqual(claimed.attempt, 1)
        self.assertIsNotNone(claimed.started_at)

    def test_same_task_id_can_reclaim_a_running_run(self):
        run = self._make_run()
        claim_run(str(run.id), "task-1")

        reclaimed = claim_run(str(run.id), "task-1")

        self.assertEqual(reclaimed.status, PipelineRun.Status.RUNNING)
        self.assertEqual(reclaimed.attempt, 2)  # each claim call bumps attempt

    def test_different_task_id_cannot_claim_an_already_running_run(self):
        run = self._make_run()
        claim_run(str(run.id), "task-1")

        with self.assertRaises(RunClaimRejected):
            claim_run(str(run.id), "task-2")

    def test_terminal_succeeded_run_cannot_be_reclaimed(self):
        run = self._make_run(status=PipelineRun.Status.SUCCEEDED)

        with self.assertRaises(RunClaimRejected):
            claim_run(str(run.id), "task-1")

    def test_terminal_failed_run_cannot_be_reclaimed(self):
        run = self._make_run(status=PipelineRun.Status.FAILED)

        with self.assertRaises(RunClaimRejected):
            claim_run(str(run.id), "task-1")

    def test_claim_unknown_run_id_is_rejected(self):
        import uuid

        with self.assertRaises(RunClaimRejected):
            claim_run(str(uuid.uuid4()), "task-1")

    def test_mark_run_succeeded_from_running(self):
        run = self._make_run()
        claimed = claim_run(str(run.id), "task-1")

        mark_run_succeeded(claimed)

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, PipelineRun.Status.SUCCEEDED)
        self.assertIsNotNone(claimed.finished_at)

    def test_mark_run_succeeded_from_pending_is_rejected(self):
        run = self._make_run()
        with self.assertRaises(InvalidRunTransition):
            mark_run_succeeded(run)

    def test_mark_run_failed_from_running(self):
        run = self._make_run()
        claimed = claim_run(str(run.id), "task-1")

        mark_run_failed(claimed, category="FetchFailed", message="boom", retryable=False)

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, PipelineRun.Status.FAILED)
        self.assertEqual(claimed.error_category, "FetchFailed")
        self.assertEqual(claimed.error_is_retryable, False)

    def test_succeeded_run_cannot_be_marked_failed(self):
        run = self._make_run()
        claimed = claim_run(str(run.id), "task-1")
        mark_run_succeeded(claimed)

        with self.assertRaises(InvalidRunTransition):
            mark_run_failed(claimed, category="X", message="Y", retryable=False)
