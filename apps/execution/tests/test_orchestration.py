from unittest.mock import patch

import httpx
import respx
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection, Credential
from apps.connectors.destinations.sqlserver import SqlServerDestinationConnector
from apps.execution.claims import RunClaimRejected
from apps.execution.models import PipelineRun, TaskExecution
from apps.execution.orchestration import run_pipeline
from apps.pipelines.models import Pipeline
from apps.rawstore.models import RawPayloadRecord

BASE_URL = "https://api.example-insurer.test"


class _FakeRawPayloadStore:
    """In-memory stand-in for the S3/MinIO-backed RawPayloadStore -- the
    test suite must not require a real object store."""

    def __init__(self):
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._objects[key] = data
        return f"fake://{key}"

    def get(self, uri: str) -> bytes:
        return self._objects[uri.removeprefix("fake://")]


class OrchestrationTestCase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self._fake_raw_store = _FakeRawPayloadStore()
        patcher = patch(
            "apps.rawstore.service.get_raw_payload_store", return_value=self._fake_raw_store
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_pipeline(
        self,
        *,
        destination_mapping=None,
        source_config_overrides=None,
        strict_mapping=False,
        source_auth=True,
    ) -> Pipeline:
        source_config = {"base_url": BASE_URL, "path": "/v1/policies"}
        if source_auth:
            source_config["auth_provider_type"] = "bearer"
        source_config.update(source_config_overrides or {})

        source = Connection.objects.create(
            name="Policies API",
            kind=Connection.Kind.SOURCE,
            connector_type="rest_api",
            config=source_config,
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={
                "host": "db.example-insurer.test",
                "database": "warehouse",
                "table_name": "policies",
            },
        )
        if source_auth:
            Credential.create_for_connection(source, "bearer", {"token": "tok_abc"})
        Credential.create_for_connection(
            destination, "static", {"username": "svc", "password": "pw"}
        )

        return Pipeline.objects.create(
            name="Policies sync",
            source_connection=source,
            destination_connection=destination,
            destination_mapping=(
                destination_mapping
                if destination_mapping is not None
                else {"policy_id": "id", "premium_amount": "premium"}
            ),
            strict_mapping=strict_mapping,
        )

    def _make_run(self, pipeline: Pipeline, **overrides) -> PipelineRun:
        defaults = {
            "pipeline": pipeline,
            "idempotency_key": f"run-{pipeline.id}-{overrides.get('_n', 1)}",
        }
        overrides.pop("_n", None)
        defaults.update(overrides)
        return PipelineRun.objects.create(**defaults)


class SuccessFlowTests(OrchestrationTestCase):
    @respx.mock
    def test_full_success_flow_stores_raw_before_loading_and_updates_counters(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
            return_value=httpx.Response(200, json={"results": [{"id": "P-1", "premium": 100}]})
        )
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        load_calls = []

        def fake_load(self_connector, credential, records, *, mode="append"):
            raw_exists = RawPayloadRecord.objects.filter(run=run, sequence=1).exists()
            load_calls.append((list(records), raw_exists, credential))
            return len(records)

        with patch.object(SqlServerDestinationConnector, "load", fake_load):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertEqual(run.pages_extracted, 1)
        self.assertEqual(run.records_extracted, 1)
        self.assertEqual(run.records_loaded, 1)
        self.assertEqual(run.raw_payload_count, 1)
        self.assertIsNotNone(run.finished_at)

        # raw payload was persisted BEFORE load() saw the mapped batch
        records, raw_existed_at_load_time, credential = load_calls[0]
        self.assertEqual(records, [{"policy_id": "P-1", "premium_amount": 100}])
        self.assertTrue(raw_existed_at_load_time)
        self.assertEqual(credential, {"username": "svc", "password": "pw"})

        task_execution = TaskExecution.objects.get(run=run)
        self.assertEqual(task_execution.status, TaskExecution.Status.SUCCEEDED)

    @respx.mock
    def test_multiple_pages_aggregate_counters_and_cursor(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1", "page_size": "2"}).mock(
            return_value=httpx.Response(
                200,
                json={"results": [{"id": "P-1", "premium": 100}, {"id": "P-2", "premium": 200}]},
            )
        )
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "2", "page_size": "2"}).mock(
            return_value=httpx.Response(200, json={"results": [{"id": "P-3", "premium": 300}]})
        )
        # page 3 deliberately unmocked: page 2 is short (1 < page_size=2),
        # so no further request should be made.

        pipeline = self._make_pipeline(source_config_overrides={"page_size": 2})
        run = self._make_run(pipeline)

        with patch.object(SqlServerDestinationConnector, "load", return_value=1) as mock_load:
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertEqual(run.pages_extracted, 2)
        self.assertEqual(run.records_extracted, 3)
        self.assertEqual(run.records_loaded, 2)  # fake load() returns 1 per call, 2 calls
        self.assertEqual(run.raw_payload_count, 2)
        # last page's next_cursor is None; falls back to the cursor that
        # page was fetched with, so "how far we got" is never lost.
        self.assertEqual(run.last_successful_cursor, "2")
        self.assertEqual(mock_load.call_count, 2)
        self.assertEqual(RawPayloadRecord.objects.filter(run=run).count(), 2)

    @respx.mock
    def test_empty_source_response_succeeds_with_zero_counters(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertEqual(run.pages_extracted, 0)
        self.assertEqual(run.records_extracted, 0)
        self.assertEqual(run.raw_payload_count, 0)
        self.assertEqual(RawPayloadRecord.objects.filter(run=run).count(), 0)

    @respx.mock
    def test_max_pages_boundary_succeeds_when_the_capped_page_is_also_the_last_page(self):
        # max_pages happens to equal the true number of pages -- the
        # final page is short (a real end of data), so this must succeed,
        # not be treated as a truncation. Distinguishes "the cap and the
        # natural end coincide" from "the cap cut off real data" (see
        # FailureHandlingTests.test_page_limit_exceeded_fails_run_and_preserves_prior_progress).
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1", "page_size": "2"}).mock(
            return_value=httpx.Response(
                200, json={"results": [{"id": "P-1", "premium": 1}, {"id": "P-2", "premium": 1}]}
            )
        )
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "2", "page_size": "2"}).mock(
            return_value=httpx.Response(200, json={"results": [{"id": "P-3", "premium": 1}]})
        )

        pipeline = self._make_pipeline(source_config_overrides={"max_pages": 2, "page_size": 2})
        run = self._make_run(pipeline)

        with patch.object(SqlServerDestinationConnector, "load", return_value=1):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertEqual(run.pages_extracted, 2)


class FailureHandlingTests(OrchestrationTestCase):
    @respx.mock
    def test_load_failure_after_extraction_keeps_raw_payload_and_fails_run(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
            return_value=httpx.Response(200, json={"results": [{"id": "P-1", "premium": 100}]})
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        from apps.core.exceptions import LoadFailed

        with patch.object(
            SqlServerDestinationConnector,
            "load",
            side_effect=LoadFailed("destination unreachable", retryable=False),
        ):
            with self.assertRaises(LoadFailed):
                run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.FAILED)
        self.assertEqual(run.error_category, "LoadFailed")
        self.assertEqual(run.error_is_retryable, False)
        # the page was already extracted and stored before load() ran
        self.assertTrue(RawPayloadRecord.objects.filter(run=run, sequence=1).exists())

    @respx.mock
    def test_page_limit_exceeded_fails_run_and_preserves_prior_progress(self):
        for page in (1, 2, 3):
            respx.get(f"{BASE_URL}/v1/policies", params={"page": str(page)}).mock(
                return_value=httpx.Response(
                    200, json={"results": [{"id": f"P-{page}", "premium": 100}]}
                )
            )
        # page 4 deliberately unmocked -- max_pages=3 must stop before it.

        pipeline = self._make_pipeline(source_config_overrides={"max_pages": 3})
        run = self._make_run(pipeline)

        from apps.core.exceptions import PageLimitExceededError

        with patch.object(SqlServerDestinationConnector, "load", return_value=1):
            with self.assertRaises(PageLimitExceededError):
                run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        # never SUCCEEDED -- this is the core guarantee of this fix
        self.assertEqual(run.status, PipelineRun.Status.FAILED)
        self.assertEqual(run.error_category, "PageLimitExceededError")
        self.assertEqual(run.error_is_retryable, False)
        # everything processed before the limit was hit survives
        self.assertEqual(run.pages_extracted, 3)
        self.assertEqual(run.records_extracted, 3)
        self.assertEqual(run.records_loaded, 3)
        self.assertEqual(run.raw_payload_count, 3)
        # page 3 wasn't the terminal page, so the cursor points at what
        # would have come next. A *plain* fresh manual trigger does NOT
        # resume from here -- it starts over at start_page and would
        # duplicate pages 1-3. Only an explicit continuation run
        # (apps.execution.dispatch.trigger_manual_run(..., continue_from=run),
        # see apps/execution/tests/test_dispatch.py) resumes from this
        # cursor.
        self.assertEqual(run.last_successful_cursor, "4")
        self.assertEqual(RawPayloadRecord.objects.filter(run=run).count(), 3)
        for seq in (1, 2, 3):
            self.assertTrue(RawPayloadRecord.objects.filter(run=run, sequence=seq).exists())

    @respx.mock
    def test_malformed_source_json_fails_run_non_retryably(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
            return_value=httpx.Response(200, content=b"not json")
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        from apps.core.exceptions import MalformedResponseError

        with self.assertRaises(MalformedResponseError):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=False)

        run.refresh_from_db()
        self.assertEqual(
            run.status, PipelineRun.Status.FAILED
        )  # non-retryable finalizes immediately
        self.assertEqual(run.error_category, "MalformedResponseError")
        self.assertEqual(run.error_is_retryable, False)
        self.assertEqual(RawPayloadRecord.objects.filter(run=run).count(), 0)

    def test_non_retryable_configuration_failure_before_any_http_call(self):
        pipeline = self._make_pipeline(destination_mapping={})  # no mapping configured
        run = self._make_run(pipeline)

        from apps.core.exceptions import MappingError

        with self.assertRaises(MappingError):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=False)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.FAILED)
        self.assertEqual(run.error_category, "MappingError")
        self.assertEqual(run.error_is_retryable, False)

    @respx.mock
    def test_retryable_failure_stays_running_when_not_final_attempt(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
            return_value=httpx.Response(503)
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        from apps.core.exceptions import FetchFailed

        with self.assertRaises(FetchFailed):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=False)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.RUNNING)  # NOT failed -- retries remain
        self.assertEqual(run.error_category, "FetchFailed")
        self.assertEqual(run.error_is_retryable, True)

    @respx.mock
    def test_retryable_failure_then_success_across_two_attempts(self):
        route = respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"})
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json={"results": [{"id": "P-1", "premium": 100}]}),
        ]
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "2"}).mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        from apps.core.exceptions import FetchFailed

        with patch.object(SqlServerDestinationConnector, "load", return_value=1):
            with self.assertRaises(FetchFailed):
                run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=False)

            run.refresh_from_db()
            self.assertEqual(run.status, PipelineRun.Status.RUNNING)

            # same task id -- this is the Celery retry continuation, not a
            # concurrent duplicate, so it can reclaim the still-RUNNING run.
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=False)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertEqual(run.attempt, 2)
        self.assertIsNone(run.error_category)

    @respx.mock
    def test_retry_exhaustion_marks_run_failed(self):
        respx.get(f"{BASE_URL}/v1/policies", params={"page": "1"}).mock(
            return_value=httpx.Response(503)
        )

        pipeline = self._make_pipeline()
        run = self._make_run(pipeline)

        from apps.core.exceptions import FetchFailed

        with self.assertRaises(FetchFailed):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=True)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.FAILED)
        self.assertEqual(
            run.error_is_retryable, True
        )  # still true -- it *was* retryable, we just ran out


class ClaimRejectionTests(OrchestrationTestCase):
    def test_running_pipeline_against_already_terminal_run_raises_claim_rejected(self):
        pipeline = self._make_pipeline()
        run = self._make_run(pipeline, status=PipelineRun.Status.SUCCEEDED)

        with self.assertRaises(RunClaimRejected):
            run_pipeline(run_id=str(run.id), celery_task_id="task-1", is_final_attempt=False)

        self.assertFalse(TaskExecution.objects.filter(run=run).exists())
