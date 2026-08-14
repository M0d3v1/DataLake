from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.connections.models import Connection
from apps.pipelines.models import Pipeline
from apps.sparktransform.models import SparkTransformConfig


class SparkTransformConfigModelTests(TenantTestCase):
    def _make_pipeline(self, **overrides) -> Pipeline:
        source = Connection.objects.create(
            name="Policies API", kind=Connection.Kind.SOURCE, connector_type="rest_api", config={}
        )
        destination = Connection.objects.create(
            name="Warehouse",
            kind=Connection.Kind.DESTINATION,
            connector_type="sqlserver",
            config={},
        )
        defaults = {
            "name": "Pipeline",
            "source_connection": source,
            "destination_connection": destination,
        }
        defaults.update(overrides)
        return Pipeline.objects.create(**defaults)

    def test_pipeline_defaults_to_python_processing_mode(self):
        pipeline = self._make_pipeline()
        self.assertEqual(pipeline.processing_mode, Pipeline.ProcessingMode.PYTHON)

    def test_pipeline_can_be_set_to_spark_mode(self):
        pipeline = self._make_pipeline(processing_mode=Pipeline.ProcessingMode.SPARK)
        self.assertEqual(pipeline.processing_mode, Pipeline.ProcessingMode.SPARK)

    def test_column_mappings_defaults_to_empty_dict(self):
        pipeline = self._make_pipeline()
        config = SparkTransformConfig.objects.create(pipeline=pipeline)
        self.assertEqual(config.column_mappings, {})

    def test_one_to_one_with_pipeline_accessible_both_directions(self):
        pipeline = self._make_pipeline()
        config = SparkTransformConfig.objects.create(
            pipeline=pipeline, column_mappings={"id": {"source_field": "id"}}
        )
        self.assertEqual(pipeline.spark_transform_config, config)
        self.assertEqual(config.pipeline, pipeline)

    def test_a_second_config_for_the_same_pipeline_is_rejected(self):
        pipeline = self._make_pipeline()
        SparkTransformConfig.objects.create(pipeline=pipeline)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SparkTransformConfig.objects.create(pipeline=pipeline)

    def test_deleting_pipeline_cascades_to_its_spark_transform_config(self):
        pipeline = self._make_pipeline()
        config = SparkTransformConfig.objects.create(pipeline=pipeline)
        config_id = config.id
        pipeline.delete()
        self.assertFalse(SparkTransformConfig.objects.filter(id=config_id).exists())
