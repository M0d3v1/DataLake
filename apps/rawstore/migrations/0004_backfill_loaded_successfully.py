"""Data migration: correctly initialize `loaded_successfully` for
RawPayloadRecord rows that existed before that field (and the per-page
load-tracking it represents) did.

`loaded_successfully` was added in
0003_alter_rawpayloadrecord_options_and_more with `default=False`, which
gave every pre-existing row `loaded_successfully=False` regardless of
whether its records had actually been loaded -- before that field
existed, storing a raw payload was unconditionally followed by loading it
in the same code path (there was no way for a row to exist "unloaded"
except as part of the very failure that ends the run), so that default
silently understated what these rows represent.

Correct backfill rule, applied per tenant schema: a `RawPayloadRecord`
belonging to a `PipelineRun` that reached `SUCCEEDED` was, under the
pre-hardening code, necessarily loaded -- a run cannot reach SUCCEEDED if
any page's destination load raised. Those rows are flipped to True here.
Rows belonging to a run that is FAILED (or still RUNNING/PENDING) are
left at the conservative default (False): for a FAILED run it is
genuinely ambiguous, without more information than exists in this table,
whether the specific page a load failure interrupted was one whose raw
payload had already been stored -- leaving it False is the safe
assumption ("don't know" defaults to "not confirmed loaded"), not an
error in either direction this migration can correct with confidence.
"""

from django.db import migrations


def backfill_loaded_successfully(apps, schema_editor):
    RawPayloadRecord = apps.get_model("rawstore", "RawPayloadRecord")
    RawPayloadRecord.objects.filter(run__status="succeeded", loaded_successfully=False).update(
        loaded_successfully=True
    )


def noop_reverse(apps, schema_editor):
    # Deliberately irreversible in the sense of "restore prior data": we
    # cannot recover which rows were False vs. True before this ran.
    # A no-op reverse (rather than raising) lets `migrate` roll back the
    # schema-level migrations around it without blocking on this one.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("rawstore", "0003_alter_rawpayloadrecord_options_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_loaded_successfully, noop_reverse),
    ]
