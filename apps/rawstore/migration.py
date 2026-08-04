"""Application service for migrating raw payload objects from a legacy
object-storage key to the current tenant-UUID-prefixed key -- see ADR
0007 and ADR 0008.

Tenant-scoped: every function here assumes it is called with the target
organization's schema already active (`django_tenants.utils.schema_context`
or, for a request that arrived on the tenant's own domain, the schema
django-tenants already set for that request). It does not know about
`apps.opsui`'s `RawPayloadMigrationJob` at all -- the web UI's Celery task
and the `migrate_raw_payload_storage` management command are both thin
callers around the single `run_raw_payload_migration()` entry point here,
so the inspection/migration logic itself is never duplicated between
them.

Three generations of object key have existed for a `RawPayloadRecord`:
1. **Legacy, unprefixed** (pre-hardening-pass Milestone 2): exactly
   `{pipeline_id}/{run_id}/{sequence:06d}-{checksum}.raw`, no tenant
   scoping in the key at all.
2. **Schema-prefixed** (the hardening pass, before this follow-up):
   `{schema_name}/{pipeline_id}/{run_id}/{sequence:06d}-{checksum}.raw`.
3. **Tenant-UUID-prefixed** (current):
   `{tenant_uuid}/{pipeline_id}/{run_id}/{sequence:06d}-{checksum}.raw`.

This module detects which generation a record's `storage_uri` is
currently in, purely from metadata already in the database (the record's
own `run_id`/`sequence`/`checksum_sha256` deterministically reconstruct
what the *unprefixed* key must be), and migrates generations 1 and 2 to
generation 3 by copying the object's bytes to the new key -- never
deleting the old object. Legacy-object cleanup is intentionally a
separate, not-yet-built operation; see docs/decisions/0008-internal-operator-ui.md.
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db import connection

from apps.core.exceptions import ConfigurationError, RawPayloadAccessDenied
from apps.rawstore.backends.s3 import S3RawPayloadStore
from apps.rawstore.base import get_raw_payload_store
from apps.rawstore.models import RawPayloadRecord

DEFAULT_BATCH_SIZE = 100

# How many problem-record entries (missing objects, checksum conflicts,
# unrecognized key shapes) to keep for operator review. Unbounded storage
# of these isn't needed -- a huge count is itself the actionable signal,
# and the UI/command both report the aggregate count regardless of how
# many sample entries are kept.
MAX_SAMPLE_PROBLEM_RECORDS = 200

CATEGORY_ALREADY_MIGRATED = "already_migrated"
CATEGORY_LEGACY_UNPREFIXED = "legacy_unprefixed"
CATEGORY_SCHEMA_PREFIXED = "schema_prefixed"
CATEGORY_MISSING = "missing"
CATEGORY_CHECKSUM_CONFLICT = "checksum_conflict"

_MIGRATABLE_CATEGORIES = (CATEGORY_LEGACY_UNPREFIXED, CATEGORY_SCHEMA_PREFIXED)


@dataclass
class MigrationInspectionResult:
    """Everything the operator UI's summary and the management command's
    printed report need. Every field here is a safe count/identifier --
    never raw payload bytes, storage credentials, signed URLs, secrets,
    or tokens. See `_safe_problem_entry`."""

    records_inspected: int = 0
    already_migrated: int = 0
    legacy_unprefixed: int = 0
    schema_prefixed: int = 0
    missing_objects: int = 0
    checksum_conflicts: int = 0
    ready_for_migration: int = 0
    records_migrated: int = 0
    records_skipped: int = 0
    problem_records: list[dict[str, Any]] = field(default_factory=list)


def _expected_logical_key(record: RawPayloadRecord) -> str:
    """The pre-tenant-prefix key `apps.rawstore.service.store_raw_payload`
    would have built for this record, reconstructed from the record's own
    columns -- not read from `storage_uri`."""
    return (
        f"{record.run.pipeline_id}/{record.run_id}/"
        f"{record.sequence:06d}-{record.checksum_sha256}.raw"
    )


def _safe_problem_entry(record: RawPayloadRecord, category: str, reason: str) -> dict[str, Any]:
    return {
        "record_id": str(record.id),
        "run_id": str(record.run_id),
        "sequence": record.sequence,
        "checksum_prefix": record.checksum_sha256[:12],
        "category": category,
        "reason": reason,
    }


def _classify(
    store: S3RawPayloadStore, record: RawPayloadRecord, tenant_uuid: str, schema_name: str
):
    """Returns (category, key). `key` is None when the stored URI doesn't
    correspond to any recognized key shape for this record at all -- an
    unrecognized-shape record is reported as a checksum conflict (it
    needs manual review; automatically guessing at it would be unsafe)."""
    try:
        bucket, key = store._parse_uri(record.storage_uri)
    except RawPayloadAccessDenied:
        return CATEGORY_CHECKSUM_CONFLICT, None
    if bucket != store._bucket:
        return CATEGORY_CHECKSUM_CONFLICT, None

    expected_logical = _expected_logical_key(record)
    if key == f"{tenant_uuid}/{expected_logical}":
        return CATEGORY_ALREADY_MIGRATED, key
    if key == f"{schema_name}/{expected_logical}":
        return CATEGORY_SCHEMA_PREFIXED, key
    if key == expected_logical:
        return CATEGORY_LEGACY_UNPREFIXED, key
    return CATEGORY_CHECKSUM_CONFLICT, None


def _verify_and_maybe_migrate(
    store: S3RawPayloadStore, key: str, record: RawPayloadRecord, *, dry_run: bool
) -> tuple[bool, str | None]:
    """Downloads the object at the legacy `key`, verifies its checksum
    against the record's own `checksum_sha256`, and -- if it matches and
    this isn't a dry run -- writes it to the current tenant-scoped key
    via the store's existing atomic create-only path. Returns
    (checksum_ok, new_uri_or_None)."""
    data, content_type = store.get_raw_by_key(key)
    if hashlib.sha256(data).hexdigest() != record.checksum_sha256:
        return False, None
    if dry_run:
        return True, None
    logical_key = _expected_logical_key(record)
    new_uri = store.put(logical_key, data, content_type=content_type or "application/json")
    return True, new_uri


def run_raw_payload_migration(
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress_callback: Callable[[MigrationInspectionResult], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> MigrationInspectionResult:
    """Inspect (and, unless `dry_run`, migrate) every `RawPayloadRecord`
    in the *currently active* tenant schema.

    `progress_callback`, if given, is invoked with the running (mutable,
    reused) `MigrationInspectionResult` after each batch -- callers that
    want to persist progress (apps.opsui.tasks) do so there, without this
    function knowing anything about how or where that's stored.

    `cancel_check`, if given, is polled between batches; when it returns
    True, the loop stops early (whatever was processed so far is kept --
    migration is per-record atomic, so a stopped-early pass never leaves
    a half-written object).
    """
    store = get_raw_payload_store()
    if not isinstance(store, S3RawPayloadStore):
        raise ConfigurationError(
            "raw payload migration is only implemented for the S3-compatible backend"
        )

    tenant_uuid = store._tenant_uuid()
    schema_name = connection.schema_name

    result = MigrationInspectionResult()
    records: Iterable[RawPayloadRecord] = (
        RawPayloadRecord.objects.select_related("run")
        .order_by("id")
        .iterator(chunk_size=batch_size)
    )

    since_last_callback = 0
    for record in records:
        result.records_inspected += 1
        category, key = _classify(store, record, tenant_uuid, schema_name)

        if category == CATEGORY_ALREADY_MIGRATED:
            result.already_migrated += 1
        elif key is None:
            result.checksum_conflicts += 1
            if len(result.problem_records) < MAX_SAMPLE_PROBLEM_RECORDS:
                result.problem_records.append(
                    _safe_problem_entry(
                        record,
                        CATEGORY_CHECKSUM_CONFLICT,
                        "storage_uri does not match any recognized key shape for this record",
                    )
                )
        elif not store.key_exists(key):
            result.missing_objects += 1
            if len(result.problem_records) < MAX_SAMPLE_PROBLEM_RECORDS:
                result.problem_records.append(
                    _safe_problem_entry(
                        record, CATEGORY_MISSING, "object not found in storage at the legacy key"
                    )
                )
        else:
            if category == CATEGORY_LEGACY_UNPREFIXED:
                result.legacy_unprefixed += 1
            else:
                result.schema_prefixed += 1

            checksum_ok, new_uri = _verify_and_maybe_migrate(store, key, record, dry_run=dry_run)
            if not checksum_ok:
                result.checksum_conflicts += 1
                if len(result.problem_records) < MAX_SAMPLE_PROBLEM_RECORDS:
                    result.problem_records.append(
                        _safe_problem_entry(
                            record,
                            CATEGORY_CHECKSUM_CONFLICT,
                            "downloaded object checksum does not match the recorded checksum",
                        )
                    )
            else:
                result.ready_for_migration += 1
                if not dry_run and new_uri is not None:
                    record.storage_uri = new_uri
                    record.save(update_fields=["storage_uri", "updated_at"])
                    result.records_migrated += 1

        since_last_callback += 1
        if since_last_callback >= batch_size:
            since_last_callback = 0
            if progress_callback is not None:
                progress_callback(result)
            if cancel_check is not None and cancel_check():
                break

    result.records_skipped = (
        result.already_migrated + result.missing_objects + result.checksum_conflicts
    )
    if progress_callback is not None:
        progress_callback(result)
    return result
