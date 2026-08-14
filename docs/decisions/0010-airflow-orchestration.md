# 0010: Airflow-based orchestration and scheduling (self-hosted)

## Status

Accepted.

## Context

`Pipeline.schedule_cron` has been stored since Milestone 1 with nothing
acting on it; the roadmap's placeholder plan was a Celery-beat-driven
reconciliation loop. Separately, [ADR 0009](0009-spark-backed-transform-mode.md)
scoped an opt-in Spark transform mode whose execution is inherently
multi-stage and asynchronous (extract, submit a Spark job, poll it,
load, evaluate alerts) -- a shape Celery Beat + one flat task doesn't
represent well.

Apache Airflow is a deliberate choice here, not just "a scheduler":
dynamic DAG generation, per-task retries, dependency graphs, and
sensors/deferrable operators are the right tool for "extract, then
transform, then load, then alert" as a real dependency chain, and are
also, plainly, a specific, valued skill worth demonstrating properly --
which means it has to do real orchestration work, not fire one task and
sit idle. A shallow integration (Airflow calling one opaque "run
everything" task) would satisfy neither the architecture nor that goal.

The risk this ADR exists to head off: pulling `apache-airflow` into the
same Python environment as Django, Celery, and SQLAlchemy Core. Airflow
ships strict, version-specific dependency constraints (a well-known
source of pain when mixed into an application's own venv) and has no
reason to share a process with the web/worker containers at all. Airflow
must stay operationally and dependency-wise isolated from the rest of
this platform.

## Decision

### Airflow is a separate service, in its own container, with its own metadata database -- never imports Django

The official `apache/airflow` image runs as its own `docker-compose`
services (`airflow-webserver`, `airflow-scheduler`, plus one-shot
`airflow-init`), each with **their own** Python environment. No DAG file
imports `django`, no Airflow container installs this project's
dependencies. Airflow gets its own Postgres **database** (not schema --
a separate database on the same Postgres instance for local dev, a
separate instance entirely in production) for its own metadata; this
platform's tenant schemas are never touched by Airflow directly.

**Executor: `CeleryExecutor`**, using Airflow's own Celery app (its own
broker vhost/queue on the same RabbitMQ instance this project already
runs) -- this is about how Airflow distributes *its own* tasks, and is
unrelated to `apps.execution`'s existing Celery app. The two Celery
apps never share a queue and don't know about each other; conflating
them was an earlier draft of this design and was wrong -- it would have
forced Django into Airflow's venv to let Airflow tasks call
`apps.execution.orchestration` directly in-process.

### Airflow talks to the platform only through a small, authenticated internal HTTP API -- never direct DB access, never Docker-socket exec

New module `apps.orchestration_api` (SHARED_APPS, served from the public
schema urlconf alongside `apps.opsui`) exposes exactly three endpoints,
authenticated by a static bearer token
(`settings.ORCHESTRATION_API_TOKEN`, never a browser session or CSRF --
this is machine-to-machine):

- `GET /internal/api/pipelines/` -- every active `Pipeline` across every
  tenant (schema name, pipeline id, name, `schedule_cron`). The DAG
  factory (below) calls this on every DAG-file parse cycle to decide
  what DAGs to generate. Read-only, cheap, no side effects.
- `POST /internal/api/pipelines/<pipeline_id>/trigger/` (body:
  `schema_name`, optional `continue_from_run_id`) -- wraps
  `apps.execution.dispatch.trigger_manual_run` inside
  `schema_context(schema_name)`. Returns the run id. This is the *same*
  function the web UI and `manage.py run_pipeline` call -- Airflow is a
  fourth caller of one dispatch function, not a second implementation of
  triggering.
- `GET /internal/api/runs/<run_id>/status/?schema_name=...` -- returns
  `status`/`error_category`/`error_is_retryable` (safe fields only, same
  discipline as everything else this platform ever returns about a run)
  for a polling sensor to read.

This was chosen over the two alternatives specifically to avoid their
sharper edges: mounting the Docker socket into an Airflow container (so
a `DockerOperator` could `docker compose exec`/`run` into the web
container) means that container -- one with a web UI as its attack
surface -- has host-level Docker access, a real privilege-escalation
risk; and direct Postgres access from Airflow's DAG factory would mean
raw SQL against tables owned by Django migrations, which drifts silently
the moment a migration renames a column. An HTTP API keeps one source of
truth for "what a pipeline is" and "what triggering one means" inside
Django, where the models and business logic already live, and is a
narrow, reviewable, versionable surface -- incidentally also the natural
seed of a future public API, if one gets built.

### Dynamic DAG generation: one DAG per active Pipeline, a factory, not hand-written files

Mirrors the pattern flagged in the ETL-schema reference this project
drew on as "the detail that makes it look like a platform, not a script
collection": a single `dags/pipeline_dag_factory.py` polls
`GET /internal/api/pipelines/` at parse time and builds one DAG per
active pipeline, named `pipeline_{pipeline_id}`, scheduled from that
pipeline's own `schedule_cron`. Each DAG's tasks, in dependency order:

1. `trigger_run` -- `SimpleHttpOperator` (or a thin custom operator
   wrapping `requests`) calling the trigger endpoint. Returns the new
   `run_id` via XCom.
2. `wait_for_completion` -- a sensor (`HttpSensor` or a small custom
   deferrable sensor) polling the status endpoint until the run reaches
   a terminal status. A `FAILED` run marks the Airflow task failed,
   which is what gives Airflow-level retry/alerting semantics on top of
   the platform's own retry policy (`apps.execution.retry_policy`) --
   the two retry layers serve different purposes: the inner one handles
   a single run's transient failures (a timeout, a 5xx), the outer one
   (Airflow) is what an operator sees as "this pipeline's schedule
   missed its SLA" and can alert on.
3. *(future, once ADR 0009 lands)* `submit_spark_transform` /
   `wait_for_spark_job` -- present only for pipelines with
   `processing_mode == "spark"`; absent (skipped) for every other
   pipeline. This is exactly the shape Airflow's conditional/branching
   tasks exist for, and is the reason Airflow -- not just a smarter
   Celery Beat -- was the right call once Spark mode is real.

A pipeline with no `schedule_cron` still gets a DAG (for the manual-run
task graph and future Spark-stage orchestration) but with no schedule
attached -- Airflow's own manual-trigger UI covers ad hoc runs, in
addition to the existing web UI and CLI.

### This does not replace `apps.execution`'s own Celery app or its execution guarantees

`run_pipeline_task`, `apps.execution.claims` (claim/retry/concurrency
safety), and the page-by-page extract/store/map/load loop in
`apps.execution.orchestration` are unchanged. Airflow decides *when* a
run starts and observes *whether it finished*; everything about *how* a
run actually executes -- including the immutable raw-payload guarantee
this platform's whole audit story rests on -- is exactly the code that
already exists and is already tested. Nothing about adding Airflow
touches that code path.

## Consequences

- Three fully separate pieces now compose a pipeline run: this
  platform's own Django/Celery execution engine (unchanged), Airflow
  (new: decides when, watches whether), and, once ADR 0009 lands, Spark
  (opt-in, per pipeline, for the transform stage only). Each can be
  reasoned about, tested, and demonstrated independently.
- Operational cost: a new metadata Postgres database, a new RabbitMQ
  vhost/queue, two new long-running containers (webserver, scheduler),
  and `AIRFLOW_API_TOKEN` as a new secret to manage. This is meaningful
  for a solo/small deployment -- worth it here because scheduling was
  already a committed roadmap item and the orchestration-skill goal is
  explicit, not because every ETL platform needs Airflow.
- `apps.orchestration_api`'s three endpoints are the only way anything
  outside this platform's own processes can trigger or observe a run.
  Keeping that surface narrow (three endpoints, one token, read-only
  where possible) is deliberate -- it should stay small even as more
  orchestration features get added, rather than growing into a general
  unauthenticated internal API by accretion.
- Known limitation, stated plainly: the bearer token is a single static
  secret, not per-tenant or scoped -- adequate for "one Airflow instance
  this platform's own operators run," not for letting a third party or a
  customer run their own orchestrator against this API. That would need
  real per-tenant credentials, out of scope here.
