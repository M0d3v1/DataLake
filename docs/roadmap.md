# Roadmap: deliberately deferred work

This tracks what's intentionally **not** built yet, so it isn't forgotten
or mistaken for an oversight. Each item is deferred for a stated reason,
not just "not done yet."

## Scheduling

`Pipeline.schedule_cron` is stored (Milestone 1) but nothing acts on it --
pipelines only run via manual trigger (`apps.execution.dispatch`,
`manage.py run_pipeline`). Needs: a Celery-beat-driven reconciliation loop
that reads active schedules and dispatches runs, using the same
`trigger_manual_run`-style dedup so a schedule tick never double-dispatches.

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
after a retry. It does not yet carry a watermark *between* separate runs
(e.g. "only fetch policies updated since the last successful run") --
every fresh run currently starts from `start_page`/the beginning.
`SourceCapabilities.supports_incremental` exists as the extension point;
no connector implements it yet.

## Unrestricted custom SQL

Explicitly out of scope until it gets its own security-focused design
review -- this is the highest-risk feature in the product (arbitrary SQL
execution against a customer's database). Any implementation will be
allow-listed statement shapes and least-privilege roles, not a raw SQL
textbox, per the original Milestone 1 risk assessment.

## Normal-user web UI

Everything so far is API/service-layer plus one CLI management command.
The Django templates/HTMX/Alpine UI for configuring connections,
pipelines, and viewing execution history is unbuilt.

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
