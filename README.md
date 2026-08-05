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

**Milestone 2, a correctness/security hardening pass, a follow-up
review-fix pass, and a minimal internal operator UI.** A fictional
authenticated, paginated REST API can be configured as a source, its
responses are stored immutably before mapping, mapped records are loaded
into a SQL Server destination in bounded batches, and the whole run is
manually triggerable, retried, resumed via an explicit continuation
after a page-limit failure, and recorded in execution history. Runs on
Django 5.2 LTS. See
[docs/architecture.md](docs/architecture.md) for what's built vs.
deliberately deferred, [docs/decisions/](docs/decisions/) for the
reasoning behind the major structural choices, and
[docs/roadmap.md](docs/roadmap.md) for what's coming next.

**Guarantees, stated plainly:**

- Raw source responses are stored **immutably at the application
  level**: the object key includes a checksum of the bytes and a
  tenant-UUID prefix enforced by the storage backend itself, no code path
  in this platform ever overwrites an object, a retried page either
  reuses the identical object (verified by size before reuse, not
  assumed) or preserves both versions side by side (different bytes)
  rather than one silently replacing the other, and reads verify the
  downloaded bytes' checksum before returning them. This does not by
  itself stop someone with direct storage-layer credentials (bucket
  console/API access) from deleting or overwriting an object outside
  this platform's code -- that needs the storage backend's own
  immutability controls (e.g. S3 Object Lock), which is a deployment
  decision this codebase doesn't make for you. See
  [ADR 0007](docs/decisions/0007-raw-payload-immutability.md).
- Destination loads are **at-least-once, not exactly-once** -- see
  [ADR 0005](docs/decisions/0005-execution-orchestration.md) for the
  duplicate-risk window this leaves when a worker crashes between a
  destination commit and the platform recording it.
- Reaching a source's `max_pages` safety cap while more data may still
  exist is a **failure**, not a partial success -- the run is marked
  `FAILED` (`PageLimitExceededError`, never retried automatically), and
  everything extracted up to that point stays recorded rather than being
  discarded or silently reported as a complete sync. Resolving it is an
  explicit **continuation run**
  (`trigger_manual_run(..., continue_from=failed_run)`, or
  `manage.py run_pipeline ... --continue-from <run_id>`) seeded with the
  failed run's saved cursor -- a plain fresh trigger does *not* resume
  and would duplicate every already-loaded page.
- Every outbound request (REST source, token-endpoint/multi-step
  authentication, connection tests) goes through a shared security
  policy that blocks loopback/link-local/cloud-metadata/private/multicast
  destinations by default, disables redirects, never trusts
  environment-driven proxy/netrc settings, and bounds timeouts and
  response size at a deployment-wide hard ceiling that no connection's
  own configuration can exceed (only lower). Allowlisting is layered, not
  one flag: a plain host allowlist (deployment or per-tenant) only ever
  bypasses the *private* address category, never
  loopback/link-local/cloud-metadata/multicast/unspecified, and none of
  it implicitly permits plain `http://` -- that's a separate,
  deployment-only permission. A blocked or refused redirect's target is
  never logged or echoed back in full (query strings, fragments, and
  credentials are stripped first). See
  [ADR 0006](docs/decisions/0006-outbound-http-security-policy.md) for
  exactly what it does and doesn't close (a documented, narrow
  DNS-rebinding gap remains, flagged as the next hardening task).

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

If a run failed with `PageLimitExceededError`, resume it explicitly
rather than triggering a plain fresh run (which would restart from
`start_page` and duplicate every page already loaded):

```bash
python manage.py run_pipeline <pipeline-id> --schema <tenant-schema> \
    --continue-from <failed-run-id>
```

The same continuation action is available from the operator UI's run
detail page (below), for anyone who'd rather not use a shell.

## Operator UI

A minimal internal operator UI (Django templates + HTMX, no SPA) covers
the two workflows that previously required shell access -- see
[ADR 0008](docs/decisions/0008-internal-operator-ui.md) for the full
design rationale:

- **Raw payload storage migration** (`/ops/`, platform operators only --
  `User.is_staff`): pick an organization, run a dry-run inspection, review
  a safe summary (counts only -- never raw payload contents, storage
  credentials, signed URLs, secrets, or tokens), start a real migration,
  watch progress via HTMX polling, retry a stalled job, export a summary.
  Same headless fallback as before:
  ```bash
  python manage.py migrate_raw_payload_storage --schema <tenant-schema> --dry-run
  ```
- **Pipeline run detail + continuation** (each organization's own domain,
  owners/admins/engineers): view a run's status/cursor/counters, and --
  for a run that failed with `PageLimitExceededError` -- a "Continue from
  saved cursor" action with the same safeguards as the CLI (only one
  continuation per failed run; disabled for a successful, running, or
  differently-failed run).

Both the web UI and the management commands call the exact same
application services (`apps.rawstore.migration`,
`apps.execution.dispatch`) -- neither duplicates the other's logic, and
neither is the only supported way to do either workflow.

## Stack

Python / Django (web) + Celery/RabbitMQ (worker, scheduler) + PostgreSQL
(platform metadata, via django-tenants for schema-per-tenant isolation) +
SQLAlchemy Core (external database access) + HTTPX (API access) + MinIO/S3
(immutable raw payload storage) + Django templates/HTMX (internal
operator UI -- see [ADR 0008](docs/decisions/0008-internal-operator-ui.md)).
See [docs/architecture.md](docs/architecture.md).

## Getting started

```bash
cp .env.example .env
docker compose build
docker compose up -d postgres rabbitmq minio
docker compose run --rm web python manage.py migrate_schemas --shared
docker compose run --rm web python manage.py provision_raw_store
docker compose up
```

Full walkthrough, including creating your first organization and running
tests: [docs/setup.md](docs/setup.md).

## Repository layout

```
config/            Django project settings, Celery app, URLconf (tenant +
                    public-schema urlconfs -- see ADR 0008)
apps/
  core/            Shared base models, exceptions, structured logging,
                    outbound_http (shared SSRF-hardened HTTP policy)
  orgs/            Organization (tenant), Domain, Membership
  accounts/        Custom User model
  secrets/         SecretStore interface + encrypted-field default backend
  authproviders/   AuthProvider interface + api_key/basic/bearer/token_endpoint/multi_step_token
  connectors/      SourceConnector/DestinationConnector interfaces + registry
                    + built-in REST API source, SQL Server destination
  connections/      Connection + Credential + AllowedOutboundHost models
  pipelines/       Pipeline model + destination-mapping mapper
  execution/       PipelineRun, TaskExecution, orchestration/claims/dispatch/
                    retry_policy, the Celery task, `run_pipeline` management command
  rawstore/        RawPayloadStore interface + S3/MinIO backend (tenant-scoped,
                    content-addressed, never-overwrite), migration service +
                    `provision_raw_store`/`migrate_raw_payload_storage` commands
  auditing/        AuditLog + record_audit_event()
  opsui/           Internal operator UI: raw payload migration job model +
                    Celery task, permissions, views/urls/templates (see ADR 0008)
docs/
  architecture.md  Module boundaries, key abstractions, what's deferred
  setup.md         Local development guide
  roadmap.md       Deliberately deferred work, with rationale
  decisions/       Architecture Decision Records
```

## License

MIT -- see [LICENSE](LICENSE).
