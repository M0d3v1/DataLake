from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

from apps.execution.dispatch import trigger_manual_run
from apps.pipelines.models import Pipeline


class Command(BaseCommand):
    help = "Manually trigger a pipeline run in a given tenant schema."

    def add_arguments(self, parser):
        parser.add_argument("pipeline_id", help="UUID of the Pipeline to run")
        parser.add_argument("--schema", required=True, help="Tenant schema name")
        parser.add_argument(
            "--idempotency-key",
            default=None,
            help="Optional deliberate idempotency key; reusing it dedupes dispatch.",
        )

    def handle(self, *args, **options):
        schema_name = options["schema"]
        pipeline_id = options["pipeline_id"]

        with schema_context(schema_name):
            try:
                pipeline = Pipeline.objects.get(pk=pipeline_id)
            except (Pipeline.DoesNotExist, ValidationError, ValueError) as exc:
                raise CommandError(
                    f"No pipeline with id {pipeline_id!r} in schema {schema_name!r}"
                ) from exc

            if not pipeline.is_active:
                raise CommandError(f"Pipeline {pipeline.id} is not active")

            run, dispatched = trigger_manual_run(
                pipeline, schema_name=schema_name, idempotency_key=options["idempotency_key"]
            )

        verb = "dispatched" if dispatched else "reused (not re-dispatched)"
        self.stdout.write(
            self.style.SUCCESS(
                f"PipelineRun {run.id} {verb} for pipeline {pipeline.id} "
                f"(schema={schema_name!r}, idempotency_key={run.idempotency_key!r})"
            )
        )
