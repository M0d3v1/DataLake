# Architecture

DataLake is a modular-monolith data integration platform for insurance/insurtech:
connect external REST APIs and databases, store what they return immutably,
load it into a destination database, and do all of that on a schedule with a
visible execution history. Milestone 1 built the architectural foundation
(interfaces, models, multi-tenancy). Milestone 2 makes the platform
actually run pipelines end to end: a real extract -> raw-store -> map ->
load flow with retries, resumable extraction, and duplicate-dispatch
protection. This document reflects the current (M2) state and notes
what's deliberately still deferred.

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
| `connections` | `Connection` (which connector + config) and `Credential` (which auth provider + secret ref) |
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
`page_size`-based short-page stop, a hard `max_pages` safety cap, and a
one-shot re-authenticate-and-retry on a 401/403) and `sqlserver`
(SQLAlchemy Core + pyodbc, identifier-validated schema+table, reflected
and column-checked before load, bounded batched inserts in one
transaction, append-only). PostgreSQL source/destination, SQL Server
*source* queries, and upsert/CDC are still deferred -- see the roadmap.

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
401/403 triggers `AuthProvider.invalidate()`. See
`apps/authproviders/token_support.py` and
[ADR 0005](decisions/0005-execution-orchestration.md).

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
succeeds. See [ADR 0005](decisions/0005-execution-orchestration.md) for
the full claiming/retry/duplicate-risk model, including what's
deliberately still a known limitation (a hard-killed worker leaves a run
stuck `RUNNING`; destination loads are at-least-once, not exactly-once).

## Idempotency

`PipelineRun.idempotency_key` is unique and is the mechanism
`apps.execution.dispatch.trigger_manual_run` uses to avoid creating a
second run (and enqueuing a second Celery task) for what should be one
logical execution. Within a run, `apps.execution.claims.claim_run` uses
`SELECT ... FOR UPDATE SKIP LOCKED` plus Celery-task-id ownership to stop
two workers from executing the same run concurrently, and raw-payload
metadata is `update_or_create`d on `(run, sequence)` so a retried page
never duplicates its metadata row. What is **not** idempotent: the
destination load itself (append-only INSERT, at-least-once) -- see
[ADR 0005](decisions/0005-execution-orchestration.md) for the duplicate
window this leaves and why it isn't hidden.

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
