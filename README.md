# DataLake

An open-source, production-oriented data integration platform for the
insurance and insurtech industry. Connect external REST APIs and
databases, securely configure authentication and credentials, define
ingestion pipelines, store raw source data immutably, load data into
destination databases, schedule jobs, and monitor execution history --
without writing bespoke integration code per connection.

Built for technical users with basic SQL/API knowledge, with room for
experienced engineers to go deeper (custom queries, pagination config,
restricted advanced SQL).

## Status

**Milestone 2: first working vertical slice.** A fictional authenticated,
paginated REST API can be configured as a source, its responses are
stored immutably before mapping, mapped records are loaded into a SQL
Server destination in bounded batches, and the whole run is manually
triggerable, retried, and recorded in execution history. See
[docs/architecture.md](docs/architecture.md) for what's built vs.
deliberately deferred, [docs/decisions/](docs/decisions/) for the
reasoning behind the major structural choices, and
[docs/roadmap.md](docs/roadmap.md) for what's coming next.

**Guarantees, stated plainly:** raw source responses are stored
immutably and idempotently (a retried page updates its own record, never
duplicates). Destination loads are **at-least-once, not exactly-once** --
see [ADR 0005](docs/decisions/0005-execution-orchestration.md) for the
duplicate-risk window this leaves when a worker crashes between a
destination commit and the platform recording it.

### Configuring a source (fictional example)

A `Connection` (`kind=source`, `connector_type=rest_api`):

```python
config = {
    "base_url": "https://api.example-insurer.test",
    "path": "/v1/policies",
    "method": "GET",
    "records_path": "data.items",       # or records_key for a flat response
    "page_size_param": "page_size",
    "page_size": 100,
    "max_pages": 500,
    "auth_provider_type": "bearer",     # api_key | basic | bearer | token_endpoint | multi_step_token
}
```

Its `Credential` payload for `bearer` is just `{"token": "..."}`; for
`token_endpoint` it includes the token URL, extraction rule, and optional
expiry field -- see `apps/authproviders/providers/token_endpoint.py` for
the full field list, and `multi_step.py` for the bounded, declarative
multi-step flow.

### Configuring a destination (fictional example)

A `Connection` (`kind=destination`, `connector_type=sqlserver`):

```python
config = {
    "host": "warehouse.example-insurer.test",
    "database": "analytics",
    "schema_name": "dbo",
    "table_name": "policies",
    "batch_size": 500,
}
```

The target table must already exist with columns matching the
`Pipeline.destination_mapping` destination-column names (guided table
creation is on the [roadmap](docs/roadmap.md), not built yet).

### Triggering a run

```bash
python manage.py run_pipeline <pipeline-id> --schema <tenant-schema>
```

Prints only safe identifiers (run id, pipeline id, schema, idempotency
key) -- never credentials or tokens.

## Stack

Python / Django (web) + Celery/RabbitMQ (worker, scheduler) + PostgreSQL
(platform metadata, via django-tenants for schema-per-tenant isolation) +
SQLAlchemy Core (external database access) + HTTPX (API access) + MinIO/S3
(immutable raw payload storage) + HTMX/Alpine (UI). See
[docs/architecture.md](docs/architecture.md).

## Getting started

```bash
cp .env.example .env
docker compose build
docker compose up -d postgres rabbitmq minio
docker compose run --rm web python manage.py migrate_schemas --shared
docker compose up
```

Full walkthrough, including creating your first organization and running
tests: [docs/setup.md](docs/setup.md).

## Repository layout

```
config/            Django project settings, Celery app, URLconf
apps/
  core/            Shared base models, exceptions, structured logging
  orgs/            Organization (tenant), Domain, Membership
  accounts/        Custom User model
  secrets/         SecretStore interface + encrypted-field default backend
  authproviders/   AuthProvider interface + api_key/basic/bearer/token_endpoint/multi_step_token
  connectors/      SourceConnector/DestinationConnector interfaces + registry
                    + built-in REST API source, SQL Server destination
  connections/      Connection + Credential models
  pipelines/       Pipeline model + destination-mapping mapper
  execution/       PipelineRun, TaskExecution, orchestration/claims/dispatch/
                    retry_policy, the Celery task, `run_pipeline` management command
  rawstore/        RawPayloadStore interface + S3/MinIO backend
  auditing/        AuditLog + record_audit_event()
docs/
  architecture.md  Module boundaries, key abstractions, what's deferred
  setup.md         Local development guide
  roadmap.md       Deliberately deferred work, with rationale
  decisions/       Architecture Decision Records
```

## License

MIT -- see [LICENSE](LICENSE).
