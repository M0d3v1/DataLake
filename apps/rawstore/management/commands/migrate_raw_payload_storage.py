from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

from apps.core.exceptions import ConfigurationError
from apps.rawstore.migration import run_raw_payload_migration


class Command(BaseCommand):
    help = (
        "Headless/recovery fallback for the raw payload storage migration tool "
        "(see the 'Raw payload migration' page in the operator UI for the "
        "primary, recommended workflow). Calls the exact same application "
        "service (apps.rawstore.migration.run_raw_payload_migration) the web "
        "UI's background job uses, so both produce equivalent validation and "
        "migration behavior -- this is for automation, disaster recovery, or "
        "a headless deployment with no worker/web process available."
    )

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True, help="Tenant schema name")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Inspect and report only -- never write anything to storage or the database.",
        )

    def handle(self, *args, **options):
        schema_name = options["schema"]
        dry_run = options["dry_run"]

        def _progress(result):
            self.stdout.write(f"  ...{result.records_inspected} records inspected so far")

        try:
            with schema_context(schema_name):
                result = run_raw_payload_migration(dry_run=dry_run, progress_callback=_progress)
        except ConfigurationError as exc:
            raise CommandError(str(exc)) from exc

        verb = "Dry-run inspection" if dry_run else "Migration"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} complete for schema {schema_name!r}:\n"
                f"  records inspected:     {result.records_inspected}\n"
                f"  already migrated:      {result.already_migrated}\n"
                f"  legacy unprefixed:     {result.legacy_unprefixed}\n"
                f"  schema-prefixed:       {result.schema_prefixed}\n"
                f"  missing objects:       {result.missing_objects}\n"
                f"  checksum conflicts:    {result.checksum_conflicts}\n"
                f"  ready for migration:   {result.ready_for_migration}\n"
                f"  records migrated:      {result.records_migrated}\n"
                f"  records skipped:       {result.records_skipped}"
            )
        )
        if result.missing_objects or result.checksum_conflicts:
            self.stdout.write(
                self.style.WARNING(
                    "Some records need manual review (missing objects and/or checksum "
                    "conflicts) -- see the operator UI's job detail page for a sample of "
                    "affected record identifiers, or re-run with the operator UI for a "
                    "full review before a real migration."
                )
            )
