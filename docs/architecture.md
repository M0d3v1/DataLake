# Architecture

DataLake is a modular-monolith data integration platform for insurance/insurtech:
connect external REST APIs and databases, store what they return immutably,
load it into a destination database, and do all of that on a schedule with a
visible execution history. This document covers Milestone 1 (the
architectural foundation) and notes what's deliberately deferred.

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
| `authproviders` | `AuthProvider` interface + built-in providers (API key, Basic, Bearer, token-endpoint*, multi-step*) |
| `connectors` | `SourceConnector` / `DestinationConnector` interfaces + registry + built-in connectors (REST API source, SQL Server destination) |
| `connections` | `Connection` (which connector + config) and `Credential` (which auth provider + secret ref) |
| `pipelines` | `Pipeline`: source, destination, mapping, schedule |
| `execution` | `PipelineRun`, `TaskExecution`, the Celery task(s) that drive a run |
| `rawstore` | `RawPayloadStore` interface + S3/MinIO backend, `RawPayloadRecord` metadata |
| `auditing` | `AuditLog` + `record_audit_event()` |

\* interface registered, implementation deferred to Milestone 2 -- see
`apps/authproviders/providers/token_endpoint.py` and `multi_step.py`.

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

Built-in in Milestone 1: `rest_api` (page-number pagination, JSON list
responses) and `sqlserver` (SQLAlchemy Core + pyodbc, append-only `load`).
PostgreSQL source/destination and richer REST pagination are Milestone 2/3
work.

## Authentication providers

Authentication is a fixed, registrable set of provider *classes*
(`apps/authproviders`), not user-supplied code -- this is a deliberate
security boundary, not an oversight. Each provider implements
`prepare_request(request, credential) -> request`. Built-in: `api_key`,
`basic`, `bearer`. `token_endpoint` and `multi_step_token` are registered
as extension points with `prepare_request` raising `NotImplementedError`
until Milestone 2 implements the token-exchange/refresh state machine.

## Idempotency

`PipelineRun.idempotency_key` is unique. Re-triggering the same logical run
(a scheduler retry after a worker crash, a manual re-run pointed at the same
key) is meant to be safe rather than producing a duplicate load. In
Milestone 1 this is a modeled constraint with no enforcement logic yet
(there is no real load orchestration to make idempotent); Milestone 2 wires
it into the actual extract/load chain, together with per-connector upsert
semantics where `capabilities.supports_upsert` allows it.

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

- Real extract -> raw-store -> map -> load orchestration (Milestone 2).
- Guided destination table/index creation (Milestone 2).
- Multi-step token auth and token-endpoint auth implementations (Milestone 2).
- Scheduling (Celery beat actually triggering runs), incremental sync
  watermarks, retry/backoff policy (Milestone 3).
- PostgreSQL source/destination connectors (Milestone 3).
- Restricted advanced SQL execution -- deliberately deferred pending its
  own design review; this is the highest-risk feature in the product (see
  the design proposal / risk list from Milestone 1 planning).
- A REST API for programmatic access (DRF) -- not excluded by design, just
  not needed yet; the module boundaries above don't preclude adding it.
