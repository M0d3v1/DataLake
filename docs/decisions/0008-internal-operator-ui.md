# 0008: Internal operator UI (Django templates + HTMX)

## Status

Accepted.

## Context

Two operational workflows only existed as management commands, callable
only from a shell: resolving legacy (pre-tenant-UUID) raw payload storage
keys (see [ADR 0007](0007-raw-payload-immutability.md)), and resuming a
pipeline run after it failed with `PageLimitExceededError` (see
[ADR 0005](0005-execution-orchestration.md)). Both are things a platform
operator or an organization's own admin/engineer needs to do routinely,
not something that should require shell access to the deployment. Neither
needed a SPA or a new frontend stack -- the existing stack (Django
templates, HTMX, Django's own session authentication) is enough.

Two different audiences use this UI, and they don't share a tenant
context:

- **Platform operators** need to pick *which* organization to inspect --
  a cross-tenant, "list every org" view. There's no tenant to resolve
  from a request's domain, because there isn't one yet.
- **Organization owners/admins/engineers** already have a tenant context
  -- their organization's own domain -- and should never need to specify
  which organization they mean.

## Decision

**Two URLconfs, not one, matching the two audiences.** `config/urls.py`
(`ROOT_URLCONF`) serves requests that matched a real tenant `Domain` --
`apps.opsui.urls_tenant` (pipeline run listing/detail, the continuation
action) lives here. `config/urls_public.py` (`PUBLIC_SCHEMA_URLCONF`,
enabled via `SHOW_PUBLIC_IF_NO_TENANT_FOUND = True`) serves everything
else -- `apps.opsui.urls_public` (the organization picker, the raw
payload migration tool) lives here. This is the mechanism that satisfies
"do not accept an arbitrary tenant schema string from a normal user":
the tenant-context pages never take an org/schema identifier as a URL
parameter at all -- django-tenants' `TenantMainMiddleware` has already
resolved `request.tenant` from the domain before the view runs. The
public-context pages *do* let an operator pick an organization, but only
from `Organization.objects.all()` (or, for a non-operator, only
organizations they hold a `Membership` in) -- never a free-text schema
string, and every view re-validates the picked organization against
`apps.opsui.permissions` before touching anything tenant-scoped.

**`RawPayloadMigrationJob` lives in the public schema
(`SHARED_APPS`), not the tenant schema**, even though the work it tracks
is entirely tenant-scoped: an operator needs to list/monitor jobs across
every organization from one place, which a per-tenant-schema table can't
do without iterating every schema. The actual inspection/migration logic
(`apps.rawstore.migration.run_raw_payload_migration`) still runs with the
target organization's tenant schema active
(`django_tenants.utils.schema_context`) -- reading/writing
`RawPayloadRecord` and the S3 backend requires that. Writing progress
onto the job row from inside that schema context works without extra
schema-switching, because a `SHARED_APPS` table stays reachable via
Postgres's search_path regardless of which schema is currently "active"
(the same fact that lets `S3RawPayloadStore._tenant_uuid()` query
`Organization` from inside a tenant schema -- see ADR 0007).

**Same application service, three callers, one implementation.**
`apps.rawstore.migration.run_raw_payload_migration()` is the only place
the classify/verify/migrate logic exists. `apps.opsui.tasks
.run_raw_payload_migration_task` (the web UI's Celery background job)
and `manage.py migrate_raw_payload_storage` (the headless/recovery
fallback) both call it directly -- neither reimplements any part of it.
The command takes a raw `--schema` argument (not an organization
picker) and runs synchronously in-process; it is explicitly the
"no web process, no worker, no job bookkeeping" escape hatch, not the
primary workflow. The same split exists for continuation:
`apps.execution.dispatch.trigger_manual_run(..., continue_from=...)` is
the one implementation both `apps.opsui.views.run_continue` and
`manage.py run_pipeline --continue-from` call.

**Long-running work never blocks a request.** Both dry-run inspection
and a real migration run through the same `RawPayloadMigrationJob` +
Celery task path (`apps.opsui.tasks.run_raw_payload_migration_task`) --
there's no separate "fast synchronous dry-run" code path, so dry-run and
real-migration reporting can never drift apart. `RawPayloadMigrationJob
.Status` (`pending`/`running`/`succeeded`/`partially_failed`/`failed`/
`cancelled`) and a `one_active_raw_payload_migration_job_per_org` DB
constraint (`UniqueConstraint` with a `status__in=[pending, running]`
condition) mean a second submission for the same organization can never
create a conflicting job -- it's redirected to the existing one instead,
matching `PipelineRun.idempotency_key`'s dedup pattern in ADR 0005.
Progress is surfaced via HTMX polling (`hx-trigger="every 2s"` on a
fragment that stops re-declaring the trigger once the job reaches a
terminal status) -- no JavaScript framework, no websockets.

**Retry is "start over," not "resume a partial write," because the
underlying operation is idempotent.** `apps.rawstore.migration`'s
create-only, checksum-verified writes (ADR 0007) mean a fresh full pass
after any interruption -- a clean failure, a cancellation, or a worker
that died mid-run -- can never duplicate or corrupt work a prior pass
already did; an already-migrated record is a near-zero-cost no-op on the
next pass. A job stuck `RUNNING` past a documented staleness threshold
(`apps.opsui.models.STALE_JOB_THRESHOLD`, 15 minutes) can be retried by
marking it `CANCELLED` and starting a replacement -- the same honest,
operator-driven-recovery posture ADR 0005 already takes for a
hard-killed worker leaving a `PipelineRun` stuck `RUNNING`; this is not a
heartbeat/lease system, and doesn't pretend to be one.

**Never displayed: raw payload contents, object-store credentials,
signed URLs, secrets, tokens.** `RawPayloadMigrationJob
.sample_problem_records` only ever holds a bounded list of safe
identifiers (record id, run id, sequence, a truncated checksum prefix,
category, a short human-readable reason) -- see
`apps.rawstore.migration._safe_problem_entry`. Job failure messages go
through the same `apps.core.redaction.sanitize_error_message` pass as
`PipelineRun.error_message`. The export/summary view
(`apps.opsui.views.migration_job_export`) renders from exactly the same
sanitized fields, so there's no separate "export" path that could leak
something the on-screen summary already withheld.

**Legacy objects are never deleted.** Migration only ever *adds* a new,
tenant-UUID-prefixed object and updates `RawPayloadRecord.storage_uri` to
point at it -- the old object stays exactly where it was. Cleaning up
superseded legacy objects is explicitly out of scope here, a separate,
not-yet-built, explicitly-controlled operation -- consistent with ADR
0007's "never overwrite, never implicitly delete" posture for raw
payload storage.

**Authorization, not just authentication.**
`apps.opsui.permissions.is_platform_operator` is `User.is_staff` -- the
existing Django flag, not a new field -- and is the only thing that may
trigger a raw payload migration (dry-run or real). Viewing an
organization's migration status additionally allows that organization's
own owners/admins (`apps.opsui.permissions.require_org_role`).
Continuing a failed pipeline run additionally allows engineers
(`CONTINUATION_ROLES`); analysts are read-only everywhere. Every
mutating endpoint is POST-only (`@require_POST`) and relies on Django's
ordinary session-cookie CSRF protection -- nothing here disables it.

**Every significant action is audited.** Dry-run requested, migration
started/completed/failed, continuation requested/created/reused all call
`apps.auditing.service.record_audit_event` (the same tenant-scoped
`AuditLog` every other domain action uses) with only safe identifiers
and counts in `metadata` -- never raw payload content or storage
credentials. Because `AuditLog` is tenant-scoped but the migration
views run in the public schema, `apps.opsui.audit
.record_migration_audit_event` enters the target organization's schema
(`schema_context`) just long enough to write the row, so each org's own
audit trail reflects operator actions taken on its data.

## Consequences

- Both operational workflows (raw payload migration, pipeline
  continuation) are reachable without shell access, by the people who
  actually need them day to day (org admins/engineers for continuation;
  platform operators for storage migration), while the management
  commands remain a supported fallback for automation and disaster
  recovery -- neither interface can drift from the other's behavior,
  because both call the same application services.
- The public/tenant urlconf split means the tenant-context pages
  (pipeline runs, continuation) get django-tenants' domain-based tenant
  isolation "for free" -- there is no code path in `apps.opsui.urls_tenant`
  views that could be tricked into operating on a different tenant's
  data via a crafted URL parameter, because no such parameter exists.
- `RawPayloadMigrationJob` living in the public schema is a deliberate
  asymmetry with `PipelineRun`/`RawPayloadRecord` (both tenant-scoped) --
  future cross-tenant operator tooling should default to this same
  pattern (public-schema job-tracking model, tenant-schema business
  data, `schema_context` bridging the two) rather than inventing a new
  one per feature.
- This is a genuinely new, un-templated part of the codebase (no prior
  Django templates/HTMX existed) -- styling is minimal, hand-rolled CSS,
  deliberately not "polished product design," matching the brief's
  "must be understandable, safe, and usable... does not need polished
  product design."
