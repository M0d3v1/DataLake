"""Add Organization.tenant_uuid as a genuinely unique, stable tenant
identifier (see apps.rawstore.backends.s3 / ADR 0007) -- item 5 of the
correctness/security hardening follow-up.

A callable default (`uuid.uuid4`) does not generate distinct values per
existing row through a single ALTER TABLE; Django's own migration
questioner flags this. Following the documented pattern
(https://docs.djangoproject.com/en/5.2/howto/writing-migrations/#migrations-that-add-unique-fields):
add the field nullable first, backfill a fresh UUID per existing row in
Python, then tighten it to NOT NULL + UNIQUE.
"""

import uuid

from django.db import migrations, models


def backfill_tenant_uuids(apps, schema_editor):
    Organization = apps.get_model("orgs", "Organization")
    for organization in Organization.objects.filter(tenant_uuid__isnull=True):
        organization.tenant_uuid = uuid.uuid4()
        organization.save(update_fields=["tenant_uuid"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="tenant_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True),
        ),
        migrations.RunPython(backfill_tenant_uuids, noop_reverse),
        migrations.AlterField(
            model_name="organization",
            name="tenant_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
