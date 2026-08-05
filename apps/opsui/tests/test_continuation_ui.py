from unittest.mock import patch

from django.test import Client

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.opsui.tests.base import OpsUITestCase, tenant_reverse
from apps.orgs.models import Membership
from apps.pipelines.models import Pipeline


class ContinuationUITestCase(OpsUITestCase):
    def _make_pipeline(self) -> Pipeline:
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
        return Pipeline.objects.create(
            name="Policies sync",
            source_connection=source,
            destination_connection=destination,
            destination_mapping={"id": "id"},
        )

    def _make_failed_run(self, pipeline: Pipeline, **overrides) -> PipelineRun:
        defaults = {
            "pipeline": pipeline,
            "idempotency_key": f"failed-{pipeline.id}-{PipelineRun.objects.count()}",
            "status": PipelineRun.Status.FAILED,
            "error_category": "PageLimitExceededError",
            "last_successful_cursor": "4",
            "pages_extracted": 3,
            "records_loaded": 3,
        }
        defaults.update(overrides)
        return PipelineRun.objects.create(**defaults)


class RunDetailAccessTests(ContinuationUITestCase):
    def test_unauthenticated_request_redirects_to_login(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)

        response = self.tenant_get(
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.headers["Location"])

    def test_analyst_can_view_but_not_continue(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        analyst = self.make_member("analyst", Membership.Role.ANALYST)
        self.login(analyst)

        response = self.tenant_get(
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_continue"])

    def test_non_member_cannot_view_run_detail(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        outsider = self.make_user("outsider")
        self.login(outsider)

        response = self.tenant_get(
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
        )

        self.assertEqual(response.status_code, 403)

    def test_member_of_another_organization_cannot_reach_this_tenants_run(self):
        other_org = self.create_other_org(
            "other_org_continuation_test", "Other Org", "other-org-continuation-test"
        )
        try:
            other_org_engineer = self.make_member(
                "other-org-engineer", Membership.Role.ENGINEER, organization=other_org
            )
            self.login(other_org_engineer)
            pipeline = self._make_pipeline()
            run = self._make_failed_run(pipeline)

            response = self.tenant_get(
                tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
            )

            # This tenant's Membership table has no row for this user at
            # all -- membership is per (user, organization), so a role in
            # a *different* org grants nothing here.
            self.assertEqual(response.status_code, 403)
        finally:
            self.delete_other_org(other_org)


class RunContinuationTests(ContinuationUITestCase):
    def test_successful_continuation_dispatches_a_new_run_and_redirects(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        with patch("apps.execution.dispatch.run_pipeline_task") as mock_task:
            response = self.tenant_post(
                tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
            )

        new_run = PipelineRun.objects.exclude(id=run.id).get(pipeline=pipeline)
        self.assertEqual(new_run.continued_from_id, run.id)
        self.assertEqual(new_run.last_successful_cursor, "4")
        mock_task.delay.assert_called_once()
        self.assertRedirects(
            response,
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=new_run.id),
            fetch_redirect_response=False,
        )

    def test_reusing_an_existing_continuation_does_not_create_a_second_one(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        with patch("apps.execution.dispatch.run_pipeline_task"):
            self.tenant_post(
                tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
            )
            second_response = self.tenant_post(
                tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
            )

        self.assertEqual(PipelineRun.objects.filter(continued_from=run).count(), 1)
        self.assertEqual(second_response.status_code, 302)

    def test_run_detail_shows_existing_continuation_instead_of_the_action(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        with patch("apps.execution.dispatch.run_pipeline_task"):
            self.tenant_post(
                tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
            )

        response = self.tenant_get(
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
        )

        self.assertFalse(response.context["can_continue"])
        self.assertIsNotNone(response.context["existing_continuation"])

    def test_analyst_cannot_trigger_continuation(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        analyst = self.make_member("analyst", Membership.Role.ANALYST)
        self.login(analyst)

        response = self.tenant_post(
            tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PipelineRun.objects.filter(continued_from=run).exists())

    def test_cannot_continue_a_successful_run(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(
            pipeline, status=PipelineRun.Status.SUCCEEDED, error_category=None
        )
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        response = self.tenant_get(
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
        )
        self.assertFalse(response.context["is_continuable"])

        with patch("apps.execution.dispatch.run_pipeline_task"):
            post_response = self.tenant_post(
                tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
            )
        self.assertEqual(post_response.status_code, 302)
        self.assertFalse(PipelineRun.objects.filter(continued_from=run).exists())

    def test_cannot_continue_a_running_run(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(
            pipeline, status=PipelineRun.Status.RUNNING, error_category=None
        )
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        response = self.tenant_get(
            tenant_reverse("opsui_tenant:run_detail", pipeline_id=pipeline.id, run_id=run.id)
        )
        self.assertFalse(response.context["is_continuable"])

    def test_cannot_continue_a_run_with_a_different_failure_category(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline, error_category="FetchFailed")
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        with patch("apps.execution.dispatch.run_pipeline_task"):
            response = self.tenant_post(
                tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id)
            )

        self.assertFalse(PipelineRun.objects.filter(continued_from=run).exists())
        self.assertEqual(response.status_code, 302)  # redirected back with an error message

    def test_cannot_continue_a_run_belonging_to_a_different_pipeline_via_url(self):
        pipeline_a = self._make_pipeline()
        pipeline_b = self._make_pipeline()
        run_on_a = self._make_failed_run(pipeline_a)
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        self.login(engineer)

        # URL claims run_on_a belongs to pipeline_b -- must 404, not
        # silently operate on a run outside the URL's own pipeline scope.
        response = self.tenant_post(
            tenant_reverse(
                "opsui_tenant:run_continue", pipeline_id=pipeline_b.id, run_id=run_on_a.id
            )
        )
        self.assertEqual(response.status_code, 404)


class ContinuationCSRFTests(ContinuationUITestCase):
    def test_continue_requires_csrf_token(self):
        pipeline = self._make_pipeline()
        run = self._make_failed_run(pipeline)
        engineer = self.make_member("engineer", Membership.Role.ENGINEER)
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.login(username=engineer.username, password="pw")

        response = strict_client.post(
            tenant_reverse("opsui_tenant:run_continue", pipeline_id=pipeline.id, run_id=run.id),
            HTTP_HOST=self.domain.domain,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PipelineRun.objects.filter(continued_from=run).exists())
