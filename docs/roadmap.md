# Roadmap: deliberately deferred work

This tracks what's intentionally **not** built yet, so it isn't forgotten
or mistaken for an oversight. Each item is deferred for a stated reason,
not just "not done yet."

## Scheduling

**Built, not yet runtime-verified.** `Pipeline.schedule_cron` now drives
real scheduling via a self-hosted Airflow instance -- see
[ADR 0010](decisions/0010-airflow-orchestration.md) -- superseding the
earlier Celery-beat-driven-reconciliation-loop plan this section used to
describe. A dynamically generated DAG per active pipeline
(`dags/pipeline_dag_factory.py`) triggers and observes runs through a new
internal HTTP API (`apps.orchestration_api`), which itself just calls the
same `apps.execution.dispatch.trigger_manual_run` the CLI and web UI use
-- no second implementation of triggering. Airflow runs in its own
containers with its own metadata database, never importing this
project's code.

Honestly flagged: the Django side (`apps.orchestration_api`) has 16
passing tests. The Airflow side (docker-compose services, the DAG
factory) has been validated for syntax and config correctness
(`docker compose config`, `ruff check`, a Python compile check) but
**not run end-to-end** -- there is no Docker daemon available in the
environment this was built in, so `docker compose up` itself couldn't be
exercised, let alone a real Airflow scheduler parsing the DAG factory
and triggering a live run. Treat the Airflow integration as needing a
first real smoke test before relying on it.

## Spark-backed transform mode for high-volume pipelines

**Built, not yet runtime-verified.** Every pipeline still transforms
records in-process, page by page, by default (the simple mapper,
`apps.pipelines.mapping`, no expressions) -- that's the right default for
the volumes this platform targets. A pipeline that genuinely needs
distributed transforms (large joins/aggregations across a run) can opt
in per-pipeline (`Pipeline.processing_mode = "spark"`): Spark reads only
from already-checksummed immutable raw storage (never the live source
directly, via `apps.execution.orchestration._extract_only`), applies a
whitelisted (never `eval`) expression grammar
(`apps.sparktransform.expr`) to each column, and writes back through the
normal `DestinationConnector` interface (a `bulk_load()` capability, not
a bypass -- implemented for SQL Server). See
[ADR 0009](decisions/0009-spark-backed-transform-mode.md) for the full
design and build order.

Honestly flagged, same posture as Scheduling above: the Django side
(`apps.sparktransform`, plus the `bulk_load()` additions to
`apps.connectors` and the Spark-mode branch in
`apps.execution.orchestration`) has 101 new passing tests, all against
mocked backends --
no real AWS account, EMR Serverless application, or PySpark installation
was available while building this. `EmrServerlessBackend` (the boto3
`emr-serverless` client calls) and `spark_jobs/transform_job.py` (the
actual PySpark driver script the job runs) have only been
syntax/type-checked, never submitted to or run against a live Spark
cluster. Treat Spark mode as needing a first real smoke test -- a real
EMR Serverless application, a real Spark-mode pipeline, a real run --
before relying on it in production.

## PostgreSQL source and destination connectors

Only `rest_api` (source) and `sqlserver` (destination) exist. A
PostgreSQL destination would reuse the identifier-validation and
batched-transaction pattern already in `SqlServerDestinationConnector`;
a PostgreSQL *source* is new work (SQLAlchemy Core query execution
against an external DB, not an HTTP connector).

## SQL Server source queries

Currently SQL Server is destination-only. A SQL Server source connector
(custom/restricted `SELECT` queries against a source database) is
different from the destination path and needs its own design, especially
around what queries are allowed (see "unrestricted custom SQL" below).

## Guided destination table/index/relationship design

`DestinationConnector.ensure_schema()` exists in the interface
(`apps/connectors/base.py`) but every connector's default implementation
raises `NotImplementedError`. Destination tables must currently already
exist. Guided creation (columns from the mapping, basic indexes,
foreign-key relationships between destination tables) is real product
value for the "basic SQL knowledge" persona but is a meaningfully sized
feature on its own.

## Upsert and CDC

`DestinationCapabilities.supports_upsert` exists but no connector sets it
`True` yet; `load()` is append-only everywhere. Upsert would also close
most of the at-least-once duplicate-risk window documented in
[ADR 0005](decisions/0005-execution-orchestration.md). Change Data
Capture (detecting updates/deletes at the source, not just new records)
is a larger feature again and depends on source connectors that can
express "what changed since X."

## Incremental sync watermarks across runs

Milestone 2 resumes a *single* run from its own `last_successful_cursor`
after a retry, and (hardening pass) an explicit continuation run can
resume a *new* run from a prior run's cursor after a
`PageLimitExceededError` (`trigger_manual_run(..., continue_from=...)` --
see [ADR 0005](decisions/0005-execution-orchestration.md)). Neither of
those is an incremental watermark: a plain fresh manual trigger still
starts from `start_page`/the beginning, and there is still no mechanism
for "only fetch policies updated since the last *successful* run"
independent of a page-limit failure. `SourceCapabilities.supports_incremental`
exists as the extension point; no connector implements it yet.

## Unrestricted custom SQL

Explicitly out of scope until it gets its own security-focused design
review -- this is the highest-risk feature in the product (arbitrary SQL
execution against a customer's database). Any implementation will be
allow-listed statement shapes and least-privilege roles, not a raw SQL
textbox, per the original Milestone 1 risk assessment.

## Normal-user web UI

An **internal operator UI** now exists (Django templates + HTMX, no SPA
-- see [ADR 0008](decisions/0008-internal-operator-ui.md)): raw payload
storage migration for platform operators, and pipeline run
listing/detail/continuation for organization owners/admins/engineers.
What's still unbuilt is the **normal-user** (self-service, tenant-facing)
UI: configuring connections, credentials, and pipelines; guided
destination table creation; browsing full execution history beyond the
last 50 runs. The operator UI's tenant urlconf
(`apps.opsui.urls_tenant`, `config/urls.py`) and its
domain-resolves-the-tenant pattern is the natural foundation to extend
for that, rather than a separate UI stack.

## Production secret-manager integration

`apps.secrets.SecretStore` is an interface for exactly this reason,
but the only implementation is `EncryptedFieldSecretStore`
(Fernet + an application-level key -- see
[ADR 0003](decisions/0003-secret-store-abstraction.md)). A Vault/AWS
Secrets Manager-backed implementation is a drop-in addition, not a
redesign, whenever it's needed.

## Analytics dashboards

Execution history is queryable data (`PipelineRun`/`TaskExecution`/
`RawPayloadRecord`) but there's no reporting/dashboard layer over it yet.

## Operational: stale-run recovery

Noted in [ADR 0005](decisions/0005-execution-orchestration.md): a
hard-killed worker (not a clean `self.retry()`) leaves a `PipelineRun`
stuck `RUNNING` with no automatic recovery. A staleness reaper (a
scheduled task that finds runs `RUNNING` past some threshold with a dead
Celery task and either resets or fails them) is real operational
work that Milestone 2 does not include.

## Outbound HTTP: IP-pinned connections (close the DNS-rebinding gap)

Noted in [ADR 0006](decisions/0006-outbound-http-security-policy.md):
`apps.core.outbound_http` resolves and validates a destination's DNS
once, then hands the hostname (not a pinned IP) to httpx for the actual
connection -- a narrow DNS-rebinding window between validation and
connection isn't closed. Fully closing it needs a custom httpx transport
that connects to the validated IP directly while still presenting the
original hostname for TLS SNI/certificate validation. This is the
**recommended next hardening task**: a contained, well-scoped follow-up
now that the broader SSRF policy (scheme/credential/host/redirect/size
validation) is in place.

## Outbound HTTP: IP/CIDR-based allowlisting

`AllowedOutboundHost` and the deployment-level setting both match by
exact hostname string. An operator wanting to approve a whole internal
subnet (rather than naming each host) currently can't -- this is a
deliberately conservative starting point (see
[ADR 0006](decisions/0006-outbound-http-security-policy.md)), not a
long-term limitation, but CIDR-range allowlisting is real, deferred work.
