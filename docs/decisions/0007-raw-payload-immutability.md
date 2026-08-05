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

**Content-addressed keys, backend-enforced tenant prefix by stable
UUID.** `apps.rawstore.service.store_raw_payload` builds the object key
as `{pipeline}/{run}/{sequence:06d}-{checksum}.raw` -- the sha256
checksum of the payload bytes is part of the key. The storage backend
(`S3RawPayloadStore.put()`) prepends the *current* tenant's stable
`Organization.tenant_uuid` to whatever key it's given, so tenant-scoping
is enforced in one place regardless of what the caller constructs -- a
caller cannot forget to scope a key correctly, because the caller
doesn't control that part of the key at all. This deliberately uses a
dedicated UUID field, not `schema_name` (a human-chosen string that
isn't guaranteed permanent) and not `Organization.id` (a small,
sequential integer that would make object-storage prefixes a trivial
enumeration of every tenant on the deployment). Looking the UUID up
requires a direct `Organization.objects.get(schema_name=...)` query
rather than reading `connection.tenant.id` -- the actual production code
path (`django_tenants.utils.schema_context`, used by the Celery task and
`manage.py run_pipeline`) sets `connection.tenant` to a `FakeTenant` that
only carries `schema_name`, not a real `Organization` instance.

**Never overwrite -- atomic where the backend supports it, verified
either way.** `put()` first attempts an atomic conditional write
(`PutObject` with `IfNoneMatch="*"`), so two concurrent retries of the
same page can never race into an overwrite; a `PreconditionFailed`
response means the object already exists, which is treated as a no-op
only *after* a `head_object` confirms the existing object's size matches
the bytes being written -- not assumed from the key match alone. A
backend that doesn't support conditional `PutObject` falls back to a
non-atomic check-then-write (`head_object` then `put_object`), which
carries a narrow TOCTOU window between the check and the write; this is
a documented trade-off for backend compatibility, not the default path.
Since the key encodes the checksum, "already exists" can only mean
"byte-identical" barring a SHA-256 collision or storage-layer
corruption -- both of which the size-verification step is specifically
there to catch rather than silently trust.

**Checksums are verified on read, too.** `get()` recomputes the sha256
of the downloaded bytes and compares it against the checksum embedded in
the key, raising `RawPayloadIntegrityError` on a mismatch instead of
returning bytes that don't match what the key promised.

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
and the key's tenant prefix matches the *currently active* tenant's
`tenant_uuid`. A malformed URI, a foreign bucket, or a cross-tenant key
all raise `RawPayloadAccessDenied` rather than silently returning (or
worse, returning someone else's) bytes.

**Bucket provisioning is explicit, not implicit.** `S3RawPayloadStore.__init__`
no longer calls `list_buckets`/`create_bucket`. That's now
`ensure_bucket()`, invoked only by `manage.py provision_raw_store` -- an
operator-triggered, one-time-per-environment step. A `put()`/`get()`
against a missing bucket now raises `ConfigurationError` with a message
pointing at that command, instead of the platform silently provisioning
storage infrastructure as a side effect of a pipeline run.

## Application-level vs. storage-level immutability

Everything above is enforced by this codebase, for every call that goes
through it: nothing in `apps.rawstore` or its callers will ever overwrite
a previously written object, and `get()` verifies what it returns against
the checksum the key promised. That is an **application-level**
guarantee, not a storage-level one -- it protects against every code path
this platform controls, but not against something with direct
storage-layer credentials to the bucket (e.g. AWS console/API access, or
a compromised MinIO admin credential) deleting or overwriting an object
outside this code entirely. Closing that gap requires the storage
backend's own immutability controls -- e.g. S3 Object Lock in compliance
mode, or MinIO's object-locking equivalent -- configured at the bucket
level, independent of and in addition to what this ADR describes. That is
deliberately out of scope here (it's an infrastructure/deployment
concern, not an application code change) but is the honest answer to "can
someone with bucket access still delete this": today, yes; the
application layer alone cannot prevent it.

## Consequences

- "Immutable" is an accurate description of what this codebase's own
  code paths do with raw payloads: nothing in `apps.rawstore` overwrites
  a previously written object, and a read verifies rather than assumes
  that what comes back matches the checksum in its key. It is not a
  claim that the underlying storage is immutable against out-of-band
  access -- see "Application-level vs. storage-level immutability" above.
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
