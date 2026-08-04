from django.core.management.base import BaseCommand, CommandError

from apps.rawstore.base import get_raw_payload_store


class Command(BaseCommand):
    help = (
        "Explicitly provision the raw payload object store (e.g. create the "
        "configured S3/MinIO bucket if it doesn't exist yet). Bucket creation "
        "is deliberately NOT performed as a side effect of normal payload "
        "writes -- see docs/decisions/0007-raw-payload-immutability.md -- so "
        "this must be run once per environment before pipelines that write "
        "raw payloads."
    )

    def handle(self, *args, **options):
        store = get_raw_payload_store()
        ensure_bucket = getattr(store, "ensure_bucket", None)
        if ensure_bucket is None:
            raise CommandError(
                f"{type(store).__name__} does not support explicit provisioning "
                "(no ensure_bucket() method)"
            )
        ensure_bucket()
        self.stdout.write(self.style.SUCCESS("Raw payload store provisioned."))
