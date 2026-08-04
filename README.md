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

**Milestone 1: architectural foundation.** Domain models, connector and
auth-provider interfaces, the secret-store abstraction, schema-per-tenant
multi-tenancy, and a Celery execution skeleton are in place, with a minimal
REST API source connector and SQL Server destination connector. Real
extract -> raw-store -> load orchestration, scheduling, incremental sync,
and PostgreSQL connectors are upcoming milestones. See
[docs/architecture.md](docs/architecture.md) for what's built vs.
deliberately deferred, and [docs/decisions/](docs/decisions/) for the
reasoning behind the major structural choices.

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
  authproviders/   AuthProvider interface + api_key/basic/bearer providers
  connectors/      SourceConnector/DestinationConnector interfaces + registry
                    + built-in REST API source, SQL Server destination
  connections/      Connection + Credential models
  pipelines/       Pipeline model
  execution/       PipelineRun, TaskExecution, Celery task skeleton
  rawstore/        RawPayloadStore interface + S3/MinIO backend
  auditing/        AuditLog + record_audit_event()
docs/
  architecture.md  Module boundaries, key abstractions, what's deferred
  setup.md         Local development guide
  decisions/       Architecture Decision Records
```

## License

MIT -- see [LICENSE](LICENSE).
