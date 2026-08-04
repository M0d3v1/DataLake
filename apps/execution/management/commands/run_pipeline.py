from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

from apps.core.exceptions import ConfigurationError
from apps.execution.dispatch import trigger_manual_run
from apps.execution.models import PipelineRun
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
        parser.add_argument(
            "--continue-from",
            default=None,
            metavar="RUN_ID",
            help=(
                "UUID of a prior PipelineRun that failed with PageLimitExceededError. "
                "The new run resumes from its saved cursor instead of starting over."
            ),
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

            continue_from = None
            if options["continue_from"]:
                try:
                    continue_from = PipelineRun.objects.get(pk=options["continue_from"])
                except (PipelineRun.DoesNotExist, ValidationError, ValueError) as exc:
                    raise CommandError(
                        f"No PipelineRun with id {options['continue_from']!r} in schema "
                        f"{schema_name!r}"
                    ) from exc

            try:
                run, dispatched = trigger_manual_run(
                    pipeline,
                    schema_name=schema_name,
                    idempotency_key=options["idempotency_key"],
                    continue_from=continue_from,
                )
            except ConfigurationError as exc:
                raise CommandError(str(exc)) from exc

        verb = "dispatched" if dispatched else "reused (not re-dispatched)"
        continuation_note = f", continuing from run {continue_from.id}" if continue_from else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"PipelineRun {run.id} {verb} for pipeline {pipeline.id} "
                f"(schema={schema_name!r}, idempotency_key={run.idempotency_key!r})"
                f"{continuation_note}"
            )
        )
