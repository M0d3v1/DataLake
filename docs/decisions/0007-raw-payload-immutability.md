# 0007: Raw payload immutability and tenant scoping

## Status

Accepted (correctness/security hardening pass).

## Context

Milestone 2 introduced raw payload storage with a *deterministic* object
key (`{pipeline}/{run}/{sequence}.raw`) and described it as "immutable."
That description didn't match the implementation: a retried page that
returned *different* bytes than the first attempt (the source changed
between attempts, or the first attempt's response was corrupted in
transit) would silently overwrite the object at that key. That's
idempotent replacement, not immutability -- the platform would have no
way to tell, after the fact, that a payload had ever been anything other
than what's currently stored.

Separately, the object key carried no tenant boundary. Schema-per-tenant
isolation protects the *metadata* rows (`RawPayloadRecord`) correctly,
but the underlying object-storage keys lived in one flat, shared
namespace. Nothing about the key format prevented a bug elsewhere from
constructing another tenant's key and reading its payload.

## Decision

**Content-addressed keys, backend-enforced tenant prefix.**
`apps.rawstore.service.store_raw_payload` now builds the object key as
`{pipeline}/{run}/{sequence:06d}-{checksum}.raw` -- the sha256 checksum of
the payload bytes is part of the key. The storage backend
(`S3RawPayloadStore.put()`) prepends the *current* tenant schema
(`django.db.connection.schema_name`) to whatever key it's given, so
tenant-scoping is enforced in one place regardless of what the caller
constructs -- a caller cannot forget to scope a key correctly, because
the caller doesn't control that part of the key at all.

**Never overwrite.** `put()` checks whether the (tenant-prefixed,
content-addressed) key already exists (`head_object`) before writing.
Since the key encodes the checksum, "already exists" can only mean
"byte-identical" (barring a SHA-256 collision), so the check-then-write
is a genuine no-op for a retried page with identical bytes, and a
genuine new object for a retried page with different bytes -- both
versions are preserved side by side rather than one replacing the other.

**Metadata mirrors that:** `RawPayloadRecord`'s unique constraint moved
from `(run, sequence)` to `(run, sequence, checksum_sha256)`. An
identical retry hits the existing row (`get_or_create`); a conflicting
retry creates a second row for the same `(run, sequence)`, and a new
`loaded_successfully` flag -- set by the orchestrator only after that
specific version's mapped records were actually loaded at the
destination -- records which version, if either, is "the one that
counted," rather than leaving that ambiguous once more than one version
can exist per page.

**Access control on read.** `S3RawPayloadStore.get()` parses the URI and
refuses to proceed unless the bucket matches the single configured bucket
and the key's tenant prefix matches the *currently active* tenant schema.
A malformed URI, a foreign bucket, or a cross-tenant key all raise
`RawPayloadAccessDenied` rather than silently returning (or worse,
returning someone else's) bytes.

**Bucket provisioning is explicit, not implicit.** `S3RawPayloadStore.__init__`
no longer calls `list_buckets`/`create_bucket`. That's now
`ensure_bucket()`, invoked only by `manage.py provision_raw_store` -- an
operator-triggered, one-time-per-environment step. A `put()`/`get()`
against a missing bucket now raises `ConfigurationError` with a message
pointing at that command, instead of the platform silently provisioning
storage infrastructure as a side effect of a pipeline run.

## Consequences

- "Immutable" is now an accurate description everywhere this codebase
  uses the word for raw payloads: nothing overwrites a previously
  written object, ever, for any reason, and a read can only ever return
  exactly the bytes originally written under that checksum.
- A pipeline run's raw-payload history can legitimately contain more
  than one `RawPayloadRecord` for the same `(run, sequence)`. Anything
  reading raw payload history (a future audit UI, a debugging session)
  needs to be aware of this rather than assuming one row per page --
  `loaded_successfully` is the field to filter on when "what actually
  got loaded" is the question, not "what was ever fetched."
- Tenant isolation for raw payloads is now enforced at the same boundary
  (the storage backend) regardless of which code path writes or reads,
  rather than depending on every caller getting key construction right.
- Bucket provisioning must happen once, explicitly
  (`manage.py provision_raw_store`), before a fresh environment's first
  pipeline run -- documented in `docs/setup.md`. This is a deliberate
  trade against "just works on first write," in favor of the platform
  never creating storage infrastructure implicitly.
