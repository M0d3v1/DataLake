# 0009: Optional Spark-backed transform mode for high-volume pipelines

## Status

**Proposed.** Not implemented. Written to scope the work before committing
to it -- nothing here changes runtime behavior until a follow-up ADR
moves this to Accepted and the code lands.

## Context

Every pipeline today runs through one path: `apps.execution.orchestration
._extract_and_load` fetches a page, stores it immutably
(`apps.rawstore.service.store_raw_payload`), maps it in-process
(`apps.pipelines.mapping.map_records` -- deliberately *not* a
transformation language, one dotted source-field path per destination
column, no expressions), and loads it in a bounded batch. That's the
right default: simple, auditable, and fast enough for the volumes this
platform has actually been built for (paginated API pulls, thousands to
low millions of rows per run).

It stops being the right default when a pipeline's transform step is
genuinely too large or too complex for one process: distributed
aggregation, joins across pages, row counts where a single-process
Python loop becomes the bottleneck. That's a real, distinct case from
"every pipeline," and should be an **opt-in mode for specific pipelines**,
not a platform-wide rewrite -- most tenants will never need it.

This ADR exists because the temptation, when adding Spark, is to let it
become a second, parallel way of moving data that bypasses the
guarantees the rest of the platform provides. That must not happen: the
raw-payload immutability and audit trail (ADR 0007) is the product's
actual differentiator, and a transform engine that writes around it
would quietly break that promise for every pipeline that uses it.

## Decision

### Spark only ever reads from already-stored, already-checksummed raw payloads -- never from the live source

Extraction is unchanged. `source.fetch()` still runs exactly as it does
today, storing each page immutably before anything else touches it. A
Spark job's input is always the S3/MinIO prefix for a specific run --
`s3://{bucket}/{tenant_uuid}/{pipeline_id}/{run_id}/` -- generated
server-side from `PipelineRun.id`, never a path a pipeline config can
override. This means: Spark cannot be pointed at the source API
directly (no second, un-audited extraction path), and every byte Spark
transforms has already passed through the same checksum/immutability
guarantee as the simple-mapper path. The audit trail doesn't fork
depending on which transform engine ran.

### `Pipeline.processing_mode`: `"python"` (default) or `"spark"`

A new field, not a new connector type -- source/destination connectors
are unchanged; this only changes what happens to already-extracted pages
between storage and load. `SparkTransformConfig` (new model, one-to-one
with `Pipeline`, only read when `processing_mode == "spark"`) replaces
`destination_mapping` for that pipeline: same `destination_column ->
source_field` shape, plus an optional `transform_expr` per column.

### `transform_expr` is a whitelisted expression grammar, not `eval`

This is the one piece that's genuinely new attack surface, and it gets
the same treatment `apps.authproviders.token_support`'s placeholder
substitution already gets: a small, explicit, non-Turing-complete
grammar (`TRIM`, `UPPER`/`LOWER`, `CAST AS <type>`, basic arithmetic on
named fields) parsed and validated at pipeline-save time -- never
`eval`/`exec`, never a general expression language, never something that
can reach outside the record being transformed. An expression that
doesn't parse against the whitelist is a `ConfigurationError` at save
time, not a runtime surprise. This needs its own focused security review
before implementation, the same way unrestricted custom SQL does on the
roadmap -- it is not a "just add `eval(expr, row)`" afternoon task.

### Execution is asynchronous from the platform's point of view -- a new run phase, not a new run model

Spark jobs are batch, external, and don't fit the tight synchronous
per-page loop the Celery task uses today. Rather than inventing a
parallel run/history model, `PipelineRun` gains:

- `Status` is unchanged in meaning, but a `RUNNING` run for a Spark-mode
  pipeline can be in one of two *phases* (`phase` field: `extracting` |
  `transforming`) -- extraction still uses the existing claim/retry
  machinery; once extraction finishes, the Celery task submits the Spark
  job and transitions to `transforming` instead of finalizing the run.
- `spark_job_id` / `spark_job_status` (external job reference + last
  observed status), and `records_failed` (rows routed to a dead-letter
  path instead of aborting the whole batch -- mirrors `rows_failed` in
  the schema this ADR is adapting from, and is a genuine current gap:
  today a single bad record can only be all-or-nothing via
  `strict_mapping`).
- A new Celery task, `poll_spark_transform_task`, checks the external
  job's status on a backoff schedule (same `apps.execution.retry_policy`
  shape as `run_pipeline_task`) and finalizes the run
  (`mark_run_succeeded`/`mark_run_failed`) when the Spark job reports
  done -- via whichever job API the chosen deployment exposes (see
  below), not a webhook the deployment has to expose back to us unless
  that's the simpler integration for the chosen platform.

### Destination writes still go through a `DestinationConnector` -- bulk mode is a capability, not a bypass

Spark's output must still land through the existing connector interface,
not a raw JDBC write that skips `apps.connectors`. This means a new
`DestinationCapabilities.supports_bulk_load` flag and a bulk-load method
on connectors that support it (for SQL Server: staging-table + `BULK
INSERT`/bulk-copy, which is also just a faster `load()` -- same
interface, same audit hooks, different implementation). A destination
connector that doesn't support bulk load simply can't be used in Spark
mode yet; that's an explicit `ConfigurationError` at pipeline-save time,
not a silent fallback to slow per-row inserts.

### Deployment: managed/serverless Spark, not a self-hosted cluster

For a multi-tenant SaaS with wildly uneven per-tenant job sizes, a
standing Spark cluster means paying for idle capacity between runs. The
recommended target is a serverless job API (AWS EMR Serverless, Databricks
Jobs API, or GCP Dataproc Serverless -- final choice depends on which
cloud the rest of the deployment already lives on) where a job is
submitted, runs, and bills only for the run. This is a deployment/ops
decision, not a code dependency -- `apps.sparktransform` (proposed new
TENANT_APPS module holding `SparkTransformConfig` + the submit/poll
logic) should talk to whichever backend through a small interface, the
same way `apps.rawstore.base.RawPayloadStore` abstracts S3 vs. a future
alternative, so the specific vendor API isn't load-bearing everywhere.

## Consequences

- Most pipelines and most tenants never touch this -- the simple
  in-process mapper stays the default, and stays the thing new connector
  work targets first.
- The immutable raw-payload store becomes double-duty: it's both the
  audit trail (existing job) and the input dataset for Spark (new job).
  No new extraction code path, no new place bytes can enter the system
  unchecksummed.
- Real new work, roughly in build order: `SparkTransformConfig` +
  whitelisted expression grammar (needs its own security review before
  merge) -> `processing_mode`/`phase`/`spark_job_id`/`records_failed` on
  `PipelineRun` + migration -> job submission/polling against one chosen
  serverless backend -> bulk-load capability on the SQL Server connector
  (Postgres destination, still roadmap-only, would need the same
  treatment when it exists).
- This does not, by itself, decide whether to bring in Airflow. Nothing
  above needs a second orchestrator -- job submission and polling are a
  Celery task like any other. Airflow only becomes relevant if the
  product later needs DAG-level cross-job dependencies, which is a
  separate, later decision.
- Not scoped here, deliberately: guided-schema-creation for bulk-load
  staging tables, a UI for authoring `transform_expr` (vs. hand-editing
  JSON), and Postgres destination bulk-load (blocked on the Postgres
  destination connector itself, which doesn't exist yet).
