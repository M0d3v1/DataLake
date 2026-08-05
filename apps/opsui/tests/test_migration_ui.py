import hashlib
from unittest.mock import MagicMock, patch

from django.test import Client

from apps.connections.models import Connection
from apps.execution.models import PipelineRun
from apps.opsui.models import RawPayloadMigrationJob
from apps.opsui.tests.base import OpsUITestCase, public_reverse
from apps.orgs.models import Membership
from apps.pipelines.models import Pipeline
from apps.rawstore.backends.s3 import S3RawPayloadStore
from apps.rawstore.models import RawPayloadRecord


def _body(data: bytes) -> MagicMock:
    body = MagicMock()
    body.read.return_value = data
    return body


class MigrationUIPermissionTests(OpsUITestCase):
    def test_unauthenticated_request_redirects_to_login(self):
        response = self.public_get(public_reverse("opsui:org_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.headers["Location"])

    def test_non_member_is_denied_access_to_org_migration_page(self):
        outsider = self.make_user("outsider")
        self.login(outsider)

        response = self.public_get(
            public_reverse("opsui:migration_org_detail", org_id=self.tenant.id)
        )

        self.assertEqual(response.status_code, 403)

    def test_analyst_cannot_view_migration_status(self):
        analyst = self.make_member("analyst-user", Membership.Role.ANALYST)
        self.login(analyst)

        response = self.public_get(
            public_reverse("opsui:migration_org_detail", org_id=self.tenant.id)
        )

        self.assertEqual(response.status_code, 403)

    def test_org_admin_can_view_but_cannot_start_a_migration(self):
        admin = self.make_member("org-admin", Membership.Role.ADMIN)
        self.login(admin)

        detail_response = self.public_get(
            public_reverse("opsui:migration_org_detail", org_id=self.tenant.id)
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertFalse(detail_response.context["can_operate"])

        start_response = self.public_post(
            public_reverse("opsui:migration_start", org_id=self.tenant.id), {"mode": "dry_run"}
        )
        self.assertEqual(start_response.status_code, 403)

    def test_platform_operator_can_view_and_start_regardless_of_membership(self):
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        response = self.public_get(
            public_reverse("opsui:migration_org_detail", org_id=self.tenant.id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_operate"])

    def test_member_of_a_different_org_cannot_view_this_orgs_migration_status(self):
        other_org = self.create_other_org(
            "other_org_migration_test", "Other Org", "other-org-migration-test"
        )
        try:
            member_of_other_org = self.make_member(
                "other-org-admin", Membership.Role.ADMIN, organization=other_org
            )
            self.login(member_of_other_org)

            response = self.public_get(
                public_reverse("opsui:migration_org_detail", org_id=self.tenant.id)
            )
            self.assertEqual(response.status_code, 403)
        finally:
            self.delete_other_org(other_org)


class MigrationJobLifecycleTests(OpsUITestCase):
    def _make_run(self) -> PipelineRun:
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
        return PipelineRun.objects.create(pipeline=pipeline, idempotency_key="migration-ui-run")

    def _make_legacy_record(self, data: bytes = b"legacy-payload") -> RawPayloadRecord:
        run = self._make_run()
        checksum = hashlib.sha256(data).hexdigest()
        logical_key = f"{run.pipeline_id}/{run.id}/000001-{checksum}.raw"
        from django.conf import settings

        return RawPayloadRecord.objects.create(
            run=run,
            storage_uri=f"s3://{settings.RAW_STORE_BUCKET}/{logical_key}",
            size_bytes=len(data),
            checksum_sha256=checksum,
            sequence=1,
        )

    def _mocked_store(self, data: bytes):
        with patch("apps.rawstore.backends.s3.boto3.client") as mock_boto:
            store = S3RawPayloadStore()
        mock_client = mock_boto.return_value
        mock_client.head_object.return_value = {}
        mock_client.get_object.return_value = {
            "Body": _body(data),
            "ContentType": "application/json",
        }
        return store

    def test_dry_run_end_to_end_renders_summary_without_mutating(self):
        data = b"legacy-payload"
        record = self._make_legacy_record(data)
        store = self._mocked_store(data)
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        with patch("apps.rawstore.migration.get_raw_payload_store", return_value=store):
            start_response = self.public_post(
                public_reverse("opsui:migration_start", org_id=self.tenant.id), {"mode": "dry_run"}
            )

        self.assertEqual(start_response.status_code, 302)
        job = RawPayloadMigrationJob.objects.get(organization=self.tenant)
        self.assertTrue(job.dry_run)
        self.assertEqual(job.status, RawPayloadMigrationJob.Status.SUCCEEDED)
        self.assertEqual(job.ready_for_migration, 1)
        self.assertEqual(job.records_migrated, 0)

        record.refresh_from_db()
        self.assertNotIn(str(self.tenant.tenant_uuid), record.storage_uri)  # untouched

        detail_response = self.public_get(
            public_reverse("opsui:migration_job_detail", job_id=job.id)
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Dry-run inspection")
        self.assertContains(detail_response, "1")  # ready_for_migration count rendered

    def test_real_migration_confirmation_and_execution_updates_storage_uri(self):
        data = b"legacy-payload-real"
        record = self._make_legacy_record(data)
        store = self._mocked_store(data)
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        with patch("apps.rawstore.migration.get_raw_payload_store", return_value=store):
            self.public_post(
                public_reverse("opsui:migration_start", org_id=self.tenant.id), {"mode": "real"}
            )

        job = RawPayloadMigrationJob.objects.get(organization=self.tenant)
        self.assertFalse(job.dry_run)
        self.assertEqual(job.records_migrated, 1)
        record.refresh_from_db()
        self.assertIn(str(self.tenant.tenant_uuid), record.storage_uri)

    def test_duplicate_submission_does_not_create_a_second_active_job(self):
        data = b"dup-payload"
        self._make_legacy_record(data)
        store = self._mocked_store(data)
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        # First job finishes synchronously (eager Celery in tests). Simulate
        # a "still active" duplicate by creating a PENDING job directly,
        # bypassing the task so it stays non-terminal.
        job = RawPayloadMigrationJob.objects.create(
            organization=self.tenant, initiated_by=operator, dry_run=True
        )
        self.assertEqual(job.status, RawPayloadMigrationJob.Status.PENDING)

        with patch("apps.rawstore.migration.get_raw_payload_store", return_value=store):
            response = self.public_post(
                public_reverse("opsui:migration_start", org_id=self.tenant.id), {"mode": "dry_run"}
            )

        self.assertEqual(RawPayloadMigrationJob.objects.filter(organization=self.tenant).count(), 1)
        self.assertRedirects(
            response,
            public_reverse("opsui:migration_job_detail", job_id=job.id),
            fetch_redirect_response=False,
        )

    def test_htmx_progress_partial_renders_without_full_page_chrome(self):
        data = b"progress-payload"
        self._make_legacy_record(data)
        store = self._mocked_store(data)
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        with patch("apps.rawstore.migration.get_raw_payload_store", return_value=store):
            self.public_post(
                public_reverse("opsui:migration_start", org_id=self.tenant.id), {"mode": "dry_run"}
            )
        job = RawPayloadMigrationJob.objects.get(organization=self.tenant)

        response = self.public_get(
            public_reverse("opsui:migration_job_progress", job_id=job.id), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Records inspected")
        self.assertNotContains(response, "<html")  # partial fragment only

    def test_sanitized_error_is_rendered_without_secret_material(self):
        job = RawPayloadMigrationJob.objects.create(
            organization=self.tenant,
            status=RawPayloadMigrationJob.Status.FAILED,
            error_message="connection failed: Authorization: [redacted]",
        )
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        response = self.public_get(public_reverse("opsui:migration_job_detail", job_id=job.id))

        self.assertContains(response, "[redacted]")
        self.assertNotIn(b"secret-token-value", response.content)

    def test_missing_and_conflict_records_are_shown_for_review(self):
        job = RawPayloadMigrationJob.objects.create(
            organization=self.tenant,
            status=RawPayloadMigrationJob.Status.PARTIALLY_FAILED,
            missing_objects=1,
            checksum_conflicts=1,
            sample_problem_records=[
                {
                    "record_id": "11111111-1111-1111-1111-111111111111",
                    "run_id": "22222222-2222-2222-2222-222222222222",
                    "sequence": 1,
                    "checksum_prefix": "abcdef123456",
                    "category": "missing",
                    "reason": "object not found in storage at the legacy key",
                }
            ],
        )
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        response = self.public_get(public_reverse("opsui:migration_job_detail", job_id=job.id))

        self.assertContains(response, "11111111-1111-1111-1111-111111111111")
        self.assertContains(response, "object not found in storage at the legacy key")

    def test_export_summary_download_is_safe_plain_text(self):
        job = RawPayloadMigrationJob.objects.create(
            organization=self.tenant,
            status=RawPayloadMigrationJob.Status.SUCCEEDED,
            records_migrated=3,
        )
        operator = self.make_user("operator", is_staff=True)
        self.login(operator)

        response = self.public_get(public_reverse("opsui:migration_job_export", job_id=job.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(b"Records migrated:    3", response.content)


class MigrationCSRFTests(OpsUITestCase):
    def test_start_migration_requires_csrf_token(self):
        strict_client = Client(enforce_csrf_checks=True)
        operator = self.make_user("operator", is_staff=True)
        strict_client.login(username=operator.username, password="pw")

        response = strict_client.post(
            public_reverse("opsui:migration_start", org_id=self.tenant.id),
            {"mode": "dry_run"},
            HTTP_HOST="ops.test.internal",
        )

        self.assertEqual(response.status_code, 403)
