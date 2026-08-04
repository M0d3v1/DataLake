# Local setup

## Prerequisites

- Docker and Docker Compose
- (optional, for running things outside Docker) Python 3.11+

## Quick start

```bash
cp .env.example .env
# Edit .env if you want, the defaults work for local dev.

docker compose build
docker compose up -d postgres rabbitmq minio
docker compose run --rm web python manage.py migrate_schemas --shared
docker compose run --rm web python manage.py provision_raw_store
docker compose up
```

`provision_raw_store` creates the raw-payload bucket if it doesn't exist
yet. This is a deliberate, explicit, one-time step -- the platform never
creates storage infrastructure as a side effect of a normal pipeline run
writing a payload. See
[ADR 0007](decisions/0007-raw-payload-immutability.md).

This starts:

- `web` on http://localhost:8000
- `worker` (Celery)
- `scheduler` (Celery beat)
- `postgres` on :5432, `rabbitmq` (management UI on :15672), `minio` (console on :9001)

## Creating your first organization (tenant)

Every piece of domain data (connections, pipelines, runs) belongs to an
`Organization`, which maps to its own Postgres schema. Create one via the
shell:

```bash
docker compose run --rm web python manage.py shell -c "
from apps.orgs.models import Organization, Domain
org = Organization.objects.create(schema_name='acme', name='Acme Insurance', slug='acme')
Domain.objects.create(domain='acme.localhost', tenant=org, is_primary=True)
"
```

Saving the `Organization` automatically creates and migrates its schema
(`auto_create_schema = True`) -- you don't run `migrate_schemas` again for
each new tenant.

## Running tests

```bash
docker compose run --rm web pytest
```

Or outside Docker, against a local Postgres:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATALAKE_POSTGRES_HOST=localhost DATALAKE_POSTGRES_USER=datalake \
       DATALAKE_POSTGRES_PASSWORD=datalake DATALAKE_POSTGRES_DB=datalake
python manage.py migrate_schemas --shared
pytest
```

Tests that touch tenant-scoped models use `django_tenants.test.cases.TenantTestCase`,
which creates and tears down its own `test` schema per test class -- no
manual tenant setup needed for tests.

## Generating a new secret-store encryption key

The bundled `EncryptedFieldSecretStore` (see `apps/secrets`) needs a Fernet
key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put it in `.env` as `DATALAKE_SECRET_STORE_ENCRYPTION_KEY`. This is a
local-development-grade default -- see
[ADR 0003](decisions/0003-secret-store-abstraction.md) before using this in
anything resembling production.

## Linting

```bash
ruff check .
```
