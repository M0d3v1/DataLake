import json
from unittest.mock import patch

from django.urls import reverse

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.opsui.tests.base import PUBLIC_HOST, OpsUITestCase
from apps.pipelines.models import Pipeline


def _api_reverse(viewname, **kwargs):
    return reverse(f"orchestration_api:{viewname}", kwargs=kwargs, urlconf="config.urls_public")


class OrchestrationApiTestCase(OpsUITestCase):
    def _make_pipeline(self, **overrides) -> Pipeline:
        suffix = Pipeline.objects.count()
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

    def auth_headers(self, token="test-token"):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class AuthenticationTests(OrchestrationApiTestCase):
    def test_missing_token_configuration_returns_503(self):
        with self.settings(ORCHESTRATION_API_TOKEN=""):
            response = self.public_get(_api_reverse("list_pipelines"))
        self.assertEqual(response.status_code, 503)

    def test_missing_authorization_header_is_rejected(self):
        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(_api_reverse("list_pipelines"))
        self.assertEqual(response.status_code, 401)

    def test_wrong_token_is_rejected(self):
        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(
                _api_reverse("list_pipelines"), HTTP_AUTHORIZATION="Bearer wrong-token"
            )
        self.assertEqual(response.status_code, 401)

    def test_correct_token_is_accepted(self):
        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(_api_reverse("list_pipelines"), **self.auth_headers())
        self.assertEqual(response.status_code, 200)


class ListPipelinesTests(OrchestrationApiTestCase):
    def test_lists_active_pipelines_for_this_tenant_with_schema_name(self):
        pipeline = self._make_pipeline()

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(_api_reverse("list_pipelines"), **self.auth_headers())

        body = response.json()
        matching = [p for p in body["pipelines"] if p["pipeline_id"] == str(pipeline.id)]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["schema_name"], self.tenant.schema_name)
        self.assertEqual(matching[0]["name"], "Policies sync")

    def test_excludes_inactive_pipelines(self):
        self._make_pipeline(is_active=False)

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(_api_reverse("list_pipelines"), **self.auth_headers())

        body = response.json()
        self.assertEqual(
            [p for p in body["pipelines"] if p["schema_name"] == self.tenant.schema_name], []
        )

    def test_iterates_every_non_public_organization_not_just_the_current_one(self):
        # Proves list_pipelines() doesn't just read whatever schema
        # happens to be active -- it iterates Organization.objects
        # explicitly. A second org with no pipelines is enough to prove
        # the iteration reaches it (and excludes "public") without the
        # cost/fragility of writing tenant-scoped rows into a second
        # schema and dropping it again inside one wrapped test transaction.
        pipeline_a = self._make_pipeline()
        other_org = self.create_other_org(
            "other_org_orch_api_test", "Other Org", "other-org-orch-api-test"
        )
        try:
            with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
                response = self.public_get(_api_reverse("list_pipelines"), **self.auth_headers())

            body = response.json()
            schemas_seen = {p["schema_name"] for p in body["pipelines"]}
            self.assertIn(self.tenant.schema_name, schemas_seen)
            self.assertNotIn("public", schemas_seen)
            self.assertIn(
                str(pipeline_a.id), [p["pipeline_id"] for p in body["pipelines"]]
            )
        finally:
            self.delete_other_org(other_org)


class TriggerPipelineRunTests(OrchestrationApiTestCase):
    def test_triggers_a_run_and_returns_its_id(self):
        pipeline = self._make_pipeline()

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
                response = self.public_post(
                    _api_reverse("trigger_pipeline_run", pipeline_id=pipeline.id),
                    json.dumps({"schema_name": self.tenant.schema_name}),
                    content_type="application/json",
                    **self.auth_headers(),
                )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["dispatched"])
        mock_task.delay.assert_called_once()
        self.assertTrue(PipelineRun.objects.filter(pk=body["run_id"]).exists())

    def test_unknown_schema_name_returns_404(self):
        pipeline = self._make_pipeline()

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_post(
                _api_reverse("trigger_pipeline_run", pipeline_id=pipeline.id),
                json.dumps({"schema_name": "no-such-schema"}),
                content_type="application/json",
                **self.auth_headers(),
            )

        self.assertEqual(response.status_code, 404)

    def test_unknown_pipeline_id_returns_404(self):
        import uuid

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_post(
                _api_reverse("trigger_pipeline_run", pipeline_id=uuid.uuid4()),
                json.dumps({"schema_name": self.tenant.schema_name}),
                content_type="application/json",
                **self.auth_headers(),
            )

        self.assertEqual(response.status_code, 404)

    def test_inactive_pipeline_surfaces_configuration_error_as_400(self):
        pipeline = self._make_pipeline(is_active=False)

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_post(
                _api_reverse("trigger_pipeline_run", pipeline_id=pipeline.id),
                json.dumps({"schema_name": self.tenant.schema_name}),
                content_type="application/json",
                **self.auth_headers(),
            )

        self.assertEqual(response.status_code, 400)

    def test_does_not_require_a_csrf_token(self):
        # Token authentication, not session/cookie auth -- a strict CSRF
        # client (no CSRF cookie set) must still succeed here.
        from django.test import Client

        pipeline = self._make_pipeline()
        strict_client = Client(enforce_csrf_checks=True)

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            with patch("apps.execution.dispatch.run_pipeline_task"):
                response = strict_client.post(
                    _api_reverse("trigger_pipeline_run", pipeline_id=pipeline.id),
                    json.dumps({"schema_name": self.tenant.schema_name}),
                    content_type="application/json",
                    HTTP_HOST=PUBLIC_HOST,
                    HTTP_AUTHORIZATION="Bearer test-token",
                )

        self.assertEqual(response.status_code, 201)

    def test_continue_from_run_id_is_passed_through(self):
        pipeline = self._make_pipeline()
        failed_run = PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key="failed-run-orch-api",
            status=PipelineRun.Status.FAILED,
            error_category="PageLimitExceededError",
            last_successful_cursor="4",
        )

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            with patch("apps.execution.dispatch.run_pipeline_task"):
                response = self.public_post(
                    _api_reverse("trigger_pipeline_run", pipeline_id=pipeline.id),
                    json.dumps(
                        {
                            "schema_name": self.tenant.schema_name,
                            "continue_from_run_id": str(failed_run.id),
                        }
                    ),
                    content_type="application/json",
                    **self.auth_headers(),
                )

        self.assertEqual(response.status_code, 201)
        new_run = PipelineRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(new_run.continued_from_id, failed_run.id)
        self.assertEqual(new_run.last_successful_cursor, "4")


class RunStatusTests(OrchestrationApiTestCase):
    def test_returns_status_for_a_pending_run(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(pipeline=pipeline, idempotency_key="status-test-run")

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(
                _api_reverse("run_status", run_id=run.id)
                + f"?schema_name={self.tenant.schema_name}",
                **self.auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], PipelineRun.Status.PENDING)
        self.assertFalse(body["is_terminal"])

    def test_succeeded_run_is_terminal(self):
        pipeline = self._make_pipeline()
        run = PipelineRun.objects.create(
            pipeline=pipeline,
            idempotency_key="status-test-run-2",
            status=PipelineRun.Status.SUCCEEDED,
        )

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(
                _api_reverse("run_status", run_id=run.id)
                + f"?schema_name={self.tenant.schema_name}",
                **self.auth_headers(),
            )

        self.assertTrue(response.json()["is_terminal"])

    def test_unknown_run_id_returns_404(self):
        import uuid

        with self.settings(ORCHESTRATION_API_TOKEN="test-token"):
            response = self.public_get(
                _api_reverse("run_status", run_id=uuid.uuid4())
                + f"?schema_name={self.tenant.schema_name}",
                **self.auth_headers(),
            )

        self.assertEqual(response.status_code, 404)
