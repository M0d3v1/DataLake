# Architecture

DataLake is a modular-monolith data integration platform for insurance/insurtech:
connect external REST APIs and databases, store what they return immutably,
load it into a destination database, and do all of that on a schedule with a
visible execution history. Milestone 1 built the architectural foundation
(interfaces, models, multi-tenancy). Milestone 2 made the platform
actually run pipelines end to end: a real extract -> raw-store -> map ->
load flow with retries, resumable extraction, and duplicate-dispatch
protection. A subsequent correctness/security hardening pass closed three
release blockers: pagination that could silently truncate a run instead
of failing it, raw payload storage that could overwrite itself on a
conflicting retry, and outbound HTTP requests with no SSRF protection
(see [ADR 0006](decisions/0006-outbound-http-security-policy.md) and
[ADR 0007](decisions/0007-raw-payload-immutability.md)). A follow-up
review-fix pass then closed the remaining gaps found in that hardening
work: a real cross-run continuation mechanism after
`PageLimitExceededError` (Milestone 2's own resumption only covered a
retry of the *same* run), corrected outbound allowlist semantics
(private-host allowlisting no longer implicitly permits plain HTTP or
bypasses loopback/link-local/cloud-metadata), deployment-wide hard
ceilings on response size and timeouts, `trust_env=False` on every HTTP
client, a stable tenant UUID (not `schema_name`) and atomic create-only
writes for raw payload storage, a data migration correcting
`loaded_successfully` on rows that predated that field, redirect-target
sanitization before logging, and the Django 5.2 LTS upgrade. This
document reflects the current state and notes what's deliberately still
deferred.

## Process topology

One Django codebase, three runtime processes, sharing the same code and
settings:

- **web** -- Django (templates + HTMX/Alpine), the UI and (later) a REST API.
- **worker** -- Celery, executes pipeline runs.
- **scheduler** -- Celery beat, triggers scheduled runs (Milestone 3).

Infrastructure: PostgreSQL (platform metadata, one instance, many schemas --
see multi-tenancy below), RabbitMQ (Celery broker), MinIO/S3 (raw payload
storage). All defined in `docker-compose.yml` for local development.

## Multi-tenancy: schema-per-tenant

Each `Organization` (apps.orgs) maps 1:1 to a PostgreSQL schema via
[django-tenants](https://django-tenants.readthedocs.io/). `SHARED_APPS`
(users, organizations, memberships, Django auth/admin) live in the `public`
schema. `TENANT_APPS` (connections, credentials, pipelines, runs, raw
payload metadata, audit log) are migrated into *every* tenant schema
separately, so one organization's data is physically isolated from
another's inside the same Postgres instance. See
[ADR 0002](decisions/0002-schema-per-tenant-multitenancy.md).

## Module boundaries

Each Django app under `apps/` owns its own models/migrations and is only
reached through the interfaces below -- no reaching into another app's
internals.

| App | Responsibility |
|---|---|
| `core` | Shared base model mixins, domain exceptions, structured logging setup |
| `orgs` | `Organization` (tenant), `Domain`, `Membership` (role per user per org) |
| `accounts` | Custom `User` model |
| `secrets` | `SecretStore` interface + default encrypted-field backend |
| `authproviders` | `AuthProvider` interface + built-in providers (API key, Basic, Bearer, token-endpoint, multi-step) |
| `connectors` | `SourceConnector` / `DestinationConnector` interfaces + registry + built-in connectors (REST API source, SQL Server destination) |
| `connections` | `Connection` (which connector + config), `Credential` (which auth provider + secret ref), `AllowedOutboundHost` (tenant-level outbound policy allowlist) |
| `pipelines` | `Pipeline` (source, destination, mapping, schedule) + `apps.pipelines.mapping` |
| `execution` | `PipelineRun`, `TaskExecution`, `apps.execution.orchestration`/`claims`/`dispatch`/`retry_policy`, the Celery task |
| `rawstore` | `RawPayloadStore` interface + S3/MinIO backend, `RawPayloadRecord` metadata |
| `auditing` | `AuditLog` + `record_audit_event()` |

## Internal vs. external data access

Django's `DATABASES` contains exactly one entry: the platform metadata
Postgres instance, accessed via the Django ORM. Customer source/destination
databases (SQL Server, PostgreSQL) are **never** registered there. They are
reached exclusively through SQLAlchemy Core, with an `Engine` built
per-operation from decrypted, least-privilege credentials and disposed
immediately after use. See
[ADR 0004](decisions/0004-internal-external-db-separation.md).

## Connectors

A connector is the only code allowed to talk to an external system. Two
kinds, both under `apps/connectors`:

- `SourceConnector`: `test_connection(credential)`, `fetch(credential, cursor)`
  yielding `FetchResult(records, next_cursor, raw_payload)` pages.
- `DestinationConnector`: `test_connection(credential)`,
  `ensure_schema(credential, mapping)` (guided table creation -- Milestone 2),
  `load(credential, records, mode=...)`.

Connectors declare `capabilities` (e.g. `supports_incremental`,
`supports_upsert`) so orchestration can adapt instead of assuming every
connector behaves the same way -- a connector that can't do incremental
sync degrades to full refresh rather than the platform crashing or lying
about what it did.

`config` (non-secret: host, path, table name, pagination options) is set at
connector construction time. `credential` (secret: resolved via
`apps.secrets.get_secret_store()` immediately before use) is passed
per-call and never stored on the connector instance.

Built-in: `rest_api` (GET/POST, static params/headers/JSON body, a
configurable nested records path, page-number pagination with a
`page_size`-based short-page stop, a hard `max_pages` safety cap that
*fails* the run rather than silently truncating it once more data may
exist, and a one-shot re-authenticate-and-retry on a 401/403) and
`sqlserver` (SQLAlchemy Core + pyodbc, identifier-validated schema+table,
reflected and column-checked before load, bounded batched inserts in one
transaction, append-only). PostgreSQL source/destination, SQL Server
*source* queries, and upsert/CDC are still deferred -- see the roadmap.

Every outbound request the REST connector makes goes through
`apps.core.outbound_http` (see below), never through httpx directly.

## Authentication providers

Authentication is a fixed, registrable set of provider *classes*
(`apps/authproviders`), not user-supplied code -- this is a deliberate
security boundary, not an oversight. Each provider implements
`prepare_request(request, credential) -> request`. All five are real:
`api_key`, `basic`, `bearer`, `token_endpoint` (single token-acquisition
call, JSON-field or header extraction, optional expiry), and
`multi_step_token` (a bounded, declarative sequence of up to 5 HTTP
requests, later steps able to reference earlier steps' extracted values
via `{{steps.name}}` / `{{credential.field}}` placeholder substitution --
plain string substitution against a whitelist, never `eval`/`exec`/a
template engine). Token-based providers cache their acquired token for
the lifetime of one `SourceConnector.fetch()` call (one pipeline
execution's extract phase) and re-acquire on expiry or after a source
401/403 triggers `AuthProvider.invalidate()`. Every HTTP call a
token-based provider makes (token-endpoint request, each multi-step
request) goes through `apps.core.outbound_http`, the same as REST source
requests. See `apps/authproviders/token_support.py` and
[ADR 0005](decisions/0005-execution-orchestration.md).

## Outbound HTTP security policy

`apps.core.outbound_http` is the only way this platform reaches a
tenant-configured URL -- REST source requests, token-endpoint and
multi-step authentication requests, and connection tests all go through
`build_client()`/`guarded_send()` here, never bare httpx. It refuses
non-http(s) schemes and credentials embedded in a URL; resolves the host
and refuses destinations that are loopback, link-local (which covers
cloud metadata endpoints -- they all live in the link-local range),
multicast, unspecified, or private, unless explicitly allowlisted --
allowlisting is layered, not one flag: a plain host allowlist
(deployment-wide `settings.OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS`, or
per-tenant `apps.connections.models.AllowedOutboundHost`) bypasses only
the "private" category, while the higher-risk categories
(loopback/link-local/multicast/unspecified) require the separate,
deployment-only `settings.OUTBOUND_HTTP_ALLOWED_UNSAFE_HOSTS`. Plain
`http://` requires its own, separate permission
(`OUTBOUND_HTTP_ALLOW_INSECURE_HTTP` or `OUTBOUND_HTTP_INSECURE_ALLOWED_HOSTS`)
-- being on a destination allowlist never implies it. Redirects are
never followed, and a refused redirect's target is sanitized (query
string/fragment/credentials stripped) before it reaches an error message
or log line. Timeouts and response size are bounded on every call, at a
deployment-wide hard ceiling a connection's own config may lower but
never exceed. `httpx.Client` instances never trust the process
environment for proxy/netrc configuration (`trust_env=False`) -- proxy
support, if needed, is explicit and deployment-controlled. See
[ADR 0006](decisions/0006-outbound-http-security-policy.md), including
the documented DNS-rebinding residual risk this pass doesn't close.

## Mapping

`apps.pipelines.mapping.map_records` applies `Pipeline.destination_mapping`
(destination_column -> source_field, dotted paths supported) to each
extracted record: builds a new dict per record (never mutates the
source), preserves explicit `null`s, and either substitutes `None` for a
missing source field (permissive, the default) or raises `MappingError`
(`Pipeline.strict_mapping = True`). Not a transformation language --
one field reference per destination column, no expressions.

## Execution orchestration

`apps.execution.orchestration.run_pipeline` is the real flow (Celery task
in `apps.execution.tasks` is a thin boundary around it): claim the run,
resolve the source/destination connectors and credentials, then for each
page from `source.fetch()`: store the raw payload, map the records, load
them, and persist updated counters -- in that order, so a page's raw
payload always exists before (and independent of) whether its load
succeeds. A run that fails with `PageLimitExceededError` is resumed via
an explicit continuation run (`apps.execution.dispatch.trigger_manual_run(
..., continue_from=failed_run)`, or `manage.py run_pipeline
--continue-from <run_id>`), which seeds the new run's cursor/counters
from the failed one rather than restarting extraction from `start_page`.
See [ADR 0005](decisions/0005-execution-orchestration.md) for the full
claiming/retry/duplicate-risk/continuation model, including what's
deliberately still a known limitation (a hard-killed worker leaves a run
stuck `RUNNING`; destination loads are at-least-once, not exactly-once).

## Idempotency

`PipelineRun.idempotency_key` is unique and is the mechanism
`apps.execution.dispatch.trigger_manual_run` uses to avoid creating a
second run (and enqueuing a second Celery task) for what should be one
logical execution. Within a run, `apps.execution.claims.claim_run` uses
`SELECT ... FOR UPDATE SKIP LOCKED` plus Celery-task-id ownership to stop
two workers from executing the same run concurrently. Raw-payload storage
is content-addressed (the object key includes a checksum of the bytes)
and tenant-scoped by a stable `Organization.tenant_uuid` (not
`schema_name`, and not the tenant's sequential integer `id`): a retried
page with identical bytes reuses the same object -- verified, not just
assumed, via a size check before treating it as a safe reuse -- and the
same `RawPayloadRecord` row (`get_or_create` on
`(run, sequence, checksum)`); a retried page with *different* bytes gets
a genuinely new object and a second row, rather than one silently
overwriting the other -- see
[ADR 0007](decisions/0007-raw-payload-immutability.md), including the
distinction it draws between this application-level guarantee and a
storage-level one (e.g. S3 Object Lock), which this codebase doesn't
configure for you. What is **not**
idempotent: the destination load itself (append-only INSERT,
at-least-once) -- see [ADR 0005](decisions/0005-execution-orchestration.md)
for the duplicate window this leaves and why it isn't hidden.

## Logging

Framework logs (Django, Celery internals) go through the plain stdlib
console handler in `LOGGING`. Domain/application code uses
`apps.core.logging.get_logger(__name__)`, a `structlog`-based logger
configured (in `apps.core.apps.CoreConfig.ready`) to render key/value
events as single-line JSON, e.g.:

```python
log = get_logger(__name__)
log.info("pipeline_run.started", run_id=str(run.id), pipeline_id=str(run.pipeline_id))
```

so execution logs are correlated by `run_id` and machine-parseable, across
extract/load steps and across processes.

## What's deliberately not here yet

See `docs/roadmap.md` for the full list with rationale. In short:
scheduling (Celery beat actually triggering runs), PostgreSQL
source/destination connectors, SQL Server *source* queries, guided
destination table/index/relationship design, upsert/CDC and incremental
watermarks across runs, unrestricted custom SQL, a normal-user web UI,
production secret-manager integration, and analytics dashboards.
Restricted advanced SQL execution in particular is deliberately deferred
pending its own design review -- it's the highest-risk feature in the
product (see the Milestone 1 design proposal's risk list).
