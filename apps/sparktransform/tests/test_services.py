from unittest.mock import MagicMock, patch

from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection, Credential
from apps.connectors.destinations.sqlserver import SqlServerDestinationConnector
from apps.core.exceptions import ConfigurationError, TransformExpressionError
from apps.execution.models import PipelineRun
from apps.pipelines.models import Pipeline
from apps.rawstore.models import RawPayloadRecord
from apps.sparktransform.backends.base import SparkJobStatus
from apps.sparktransform.models import SparkTransformConfig
from apps.sparktransform.services import (
    _build_job_config,
    finalize_spark_transform,
    submit_spark_transform,
    validate_spark_transform_config,
)

VALID_MAPPINGS = {
    "policy_id": {"source_field": "id", "transform_expr": None},
    "premium_upper": {"source_field": "premium", "transform_expr": "UPPER(premium)"},
}


class SparkTransformServiceTestCase(TenantTestCase):
    def _make_pipeline(
        self, *, column_mappings=None, destination_config_overrides=None
    ) -> Pipeline:
        suffix = Connection.objects.count()
        source = Connection.objects.create(
            name=f"Policies API {suffix}",
            kind=Connection.Kind.SOURCE,
            connector_type="rest_api",
            config={"base_url": "https://api.example-insurer.test", "path": "/v1/policies"},
        )
        destination_config = {
            "host": "db.example-insurer.test",
            "database": "warehouse",
            "table_name": "policies",
        }
        destination_config.update(destination_config_overrides or {})
        destination = Connection.objects.create(
            name=f"Warehouse {suffix}",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config=destination_config,
        )
        Credential.create_for_connection(
            destination, "static", {"username": "svc", "password": "pw"}
        )
        pipeline = Pipeline.objects.create(
            name="Spark pipeline",
            source_connection=source,
            destination_connection=destination,
            processing_mode=Pipeline.ProcessingMode.SPARK,
        )
        if column_mappings is not None:
            SparkTransformConfig.objects.create(pipeline=pipeline, column_mappings=column_mappings)
        return pipeline

    def _make_run(self, pipeline: Pipeline, **overrides) -> PipelineRun:
        defaults = {
            "pipeline": pipeline,
            "idempotency_key": f"run-{pipeline.id}-{overrides.get('_n', 1)}",
            "status": PipelineRun.Status.RUNNING,
        }
        overrides.pop("_n", None)
        defaults.update(overrides)
        return PipelineRun.objects.create(**defaults)


# --- validate_spark_transform_config ---------------------------------------


class ValidateSparkTransformConfigTests(SparkTransformServiceTestCase):
    def test_valid_config_does_not_raise(self):
        pipeline = self._make_pipeline(column_mappings=VALID_MAPPINGS)
        validate_spark_transform_config(pipeline.spark_transform_config)

    def test_empty_column_mappings_raises(self):
        pipeline = self._make_pipeline(column_mappings={})
        with self.assertRaises(ConfigurationError):
            validate_spark_transform_config(pipeline.spark_transform_config)

    def test_invalid_destination_column_identifier_raises(self):
        pipeline = self._make_pipeline(
            column_mappings={"bad col!": {"source_field": "id", "transform_expr": None}}
        )
        with self.assertRaises(ConfigurationError):
            validate_spark_transform_config(pipeline.spark_transform_config)

    def test_missing_source_field_key_raises(self):
        pipeline = self._make_pipeline(column_mappings={"policy_id": {"transform_expr": None}})
        with self.assertRaises(ConfigurationError):
            validate_spark_transform_config(pipeline.spark_transform_config)

    def test_unsafe_source_field_raises_transform_expression_error(self):
        pipeline = self._make_pipeline(
            column_mappings={"policy_id": {"source_field": "id[0]", "transform_expr": None}}
        )
        with self.assertRaises(TransformExpressionError):
            validate_spark_transform_config(pipeline.spark_transform_config)

    def test_invalid_transform_expr_raises_transform_expression_error(self):
        pipeline = self._make_pipeline(
            column_mappings={"policy_id": {"source_field": "id", "transform_expr": "eval(x)"}}
        )
        with self.assertRaises(TransformExpressionError):
            validate_spark_transform_config(pipeline.spark_transform_config)


# --- _build_job_config -------------------------------------------------


class BuildJobConfigTests(SparkTransformServiceTestCase):
    def test_deduplicates_aliases_for_a_shared_source_field(self):
        mappings = {
            "a": {"source_field": "premium", "transform_expr": None},
            "b": {"source_field": "premium", "transform_expr": "UPPER(premium)"},
        }
        job_config = _build_job_config(mappings)
        self.assertEqual(job_config["a"]["alias"], job_config["b"]["alias"])

    def test_identity_column_compiles_to_a_bare_alias_reference(self):
        job_config = _build_job_config(
            {"policy_id": {"source_field": "id", "transform_expr": None}}
        )
        alias = job_config["policy_id"]["alias"]
        self.assertEqual(job_config["policy_id"]["spark_sql_expr"], f"`{alias}`")

    def test_transform_expr_compiles_to_a_spark_sql_function_call(self):
        job_config = _build_job_config(
            {"name_upper": {"source_field": "name", "transform_expr": "UPPER(name)"}}
        )
        alias = job_config["name_upper"]["alias"]
        self.assertEqual(job_config["name_upper"]["spark_sql_expr"], f"UPPER(`{alias}`)")


# --- submit_spark_transform ---------------------------------------------


class SubmitSparkTransformTests(SparkTransformServiceTestCase):
    def test_submits_job_updates_run_and_dispatches_polling(self):
        pipeline = self._make_pipeline(column_mappings=VALID_MAPPINGS)
        run = self._make_run(pipeline)

        fake_backend = MagicMock()
        fake_backend.submit_job.return_value = "job-123"

        with (
            patch(
                "apps.sparktransform.services.get_spark_job_backend", return_value=fake_backend
            ),
            patch("apps.sparktransform.tasks.poll_spark_transform_task") as mock_poll_task,
        ):
            submit_spark_transform(run)

        run.refresh_from_db()
        self.assertEqual(run.spark_job_id, "job-123")
        self.assertEqual(run.spark_job_status, "SUBMITTED")
        self.assertEqual(run.phase, PipelineRun.Phase.TRANSFORMING)
        mock_poll_task.delay.assert_called_once_with(
            schema_name=self.tenant.schema_name, run_id=str(run.id)
        )

        submit_kwargs = fake_backend.submit_job.call_args.kwargs
        self.assertIn(f"{pipeline.id}/{run.id}/", submit_kwargs["input_prefix"])
        self.assertTrue(submit_kwargs["output_prefix"].endswith("transformed/"))

    def test_missing_spark_transform_config_raises_configuration_error(self):
        pipeline = self._make_pipeline(column_mappings=None)  # no SparkTransformConfig at all
        run = self._make_run(pipeline)

        with self.assertRaises(ConfigurationError):
            submit_spark_transform(run)

    def test_destination_without_bulk_load_support_raises_configuration_error(self):
        pipeline = self._make_pipeline(column_mappings=VALID_MAPPINGS)
        run = self._make_run(pipeline)

        unsupported_connector = MagicMock()
        unsupported_connector.capabilities.supports_bulk_load = False

        with patch(
            "apps.sparktransform.services.get_destination_connector",
            return_value=unsupported_connector,
        ):
            with self.assertRaises(ConfigurationError):
                submit_spark_transform(run)


# --- finalize_spark_transform --------------------------------------------


class FinalizeSparkTransformTests(SparkTransformServiceTestCase):
    def test_failed_job_status_marks_run_failed(self):
        pipeline = self._make_pipeline(column_mappings=VALID_MAPPINGS)
        run = self._make_run(pipeline, phase=PipelineRun.Phase.TRANSFORMING, spark_job_id="job-1")

        job_status = SparkJobStatus(
            job_id="job-1", state="FAILED", is_terminal=True, is_success=False, error_message="boom"
        )
        finalize_spark_transform(run, job_status)

        run.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.FAILED)
        self.assertEqual(run.error_category, "SparkJobFailedError")
        self.assertEqual(run.error_message, "boom")
        self.assertEqual(run.spark_job_status, "FAILED")

    def test_successful_job_bulk_loads_output_and_marks_raw_payloads_loaded(self):
        pipeline = self._make_pipeline(column_mappings=VALID_MAPPINGS)
        run = self._make_run(pipeline, phase=PipelineRun.Phase.TRANSFORMING, spark_job_id="job-1")
        raw_record = RawPayloadRecord.objects.create(
            run=run,
            sequence=1,
            checksum_sha256="a" * 64,
            storage_uri="fake://x",
            size_bytes=10,
        )
        self.assertFalse(raw_record.loaded_successfully)

        job_status = SparkJobStatus(
            job_id="job-1", state="SUCCESS", is_terminal=True, is_success=True
        )
        fake_records = [{"policy_id": "P-1", "premium_upper": "GOLD"}]

        with (
            patch(
                "apps.sparktransform.services._load_transformed_output",
                return_value=(fake_records, 2),
            ),
            patch.object(SqlServerDestinationConnector, "bulk_load", return_value=1) as mock_bulk,
        ):
            finalize_spark_transform(run, job_status)

        run.refresh_from_db()
        raw_record.refresh_from_db()
        self.assertEqual(run.status, PipelineRun.Status.SUCCEEDED)
        self.assertEqual(run.records_loaded, 1)
        self.assertEqual(run.records_failed, 2)
        self.assertTrue(raw_record.loaded_successfully)
        mock_bulk.assert_called_once()
        self.assertEqual(mock_bulk.call_args.args[1], fake_records)
