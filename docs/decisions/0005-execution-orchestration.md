# 0005: Execution orchestration semantics

## Status

Accepted (Milestone 2).

## Context

Milestone 1 modeled `PipelineRun`/`TaskExecution` and left execution as a
stub. Milestone 2 replaces that stub with a real extract -> raw-store ->
map -> load flow running on Celery workers that serve every tenant. That
introduces real distributed-systems questions this ADR settles
deliberately, rather than leaving implicit:

1. Two workers (or a duplicate dispatch) racing to execute the same run.
2. What "retry" means when the run itself, not just the HTTP call, needs
   to resume.
3. What happens to platform state when a destination commit succeeds but
   the worker dies before recording that success.
4. What must never appear in an error message, log line, or audit event.

## Decisions

### Claiming a run: `SELECT ... FOR UPDATE SKIP LOCKED` + task-id ownership

`apps.execution.claims.claim_run` is the only code path that moves a
`PipelineRun` into `RUNNING`. It locks the row
(`select_for_update(skip_locked=True)`), so two truly simultaneous claim
attempts never block each other: the loser sees no row (still locked by
the winner's open transaction) and raises `RunClaimRejected` immediately.

A run already `RUNNING` is only re-claimable by the **same** Celery task
id. This is what makes `self.retry()` (which Celery preserves the root
task id across) a legitimate continuation of the same logical attempt,
while a second, genuinely concurrent dispatch (a different task id)
is rejected instead of executing the same run twice.

**Known limitation:** this relies on Celery's task id staying stable
across a `self.retry()` chain, which holds for the documented retry path
but not for a hard-killed worker process. If a worker is killed (not a
clean exception-driven retry) mid-run, the run is left `RUNNING` with no
automatic recovery in this milestone -- there is no heartbeat or lease
expiry yet. Recovering stuck runs is operator-driven (inspect
`PipelineRun.celery_task_id` / `updated_at`, reset manually) until a
future milestone adds a staleness reaper. This is a deliberate scope
boundary, not an oversight: building a full lease/heartbeat system is
exactly the kind of speculative infrastructure the roadmap defers.

### Terminal states are truly terminal

`SUCCEEDED` and `FAILED` are terminal. `claims.py` rejects any claim
attempt against a terminal run, and `mark_run_succeeded` /
`mark_run_failed` both assert the run is currently `RUNNING` before
transitioning, raising `InvalidRunTransition` otherwise. Nothing in the
codebase sets `PipelineRun.status` outside `apps.execution.claims`.

### Exception classification happens at the raise site

`DataLakeError` carries `retryable: bool` and `category: str`, set where
the actual cause is known (a specific HTTP status code, a specific
SQLAlchemy exception class) -- never re-inferred later from a message
string. `apps.execution.orchestration._record_failure` reads `.retryable`
to decide whether to finalize the run as `FAILED` or leave it `RUNNING`
for a Celery retry; `apps.execution.tasks.run_pipeline_task` reads the
same flag to decide whether to call `self.retry()`.

Retryable: network timeouts/connection errors, HTTP 429 and 5xx from the
source, `sqlalchemy.exc.OperationalError` from the destination
(connectivity loss, lock-wait timeout, deadlock victim -- SQLAlchemy
surfaces all of these the same way across DBAPIs). Non-retryable:
configuration errors, malformed source responses, mapping errors, schema
mismatches, `IntegrityError`, exhausted or invalid authentication, and any
exception type the codebase doesn't recognize (`UnexpectedError`,
conservatively non-retryable rather than risking an infinite retry loop
on a genuine bug).

### A run is not marked FAILED until retries are exhausted

`run_pipeline(..., is_final_attempt=...)` only finalizes a `RUNNING` run
to `FAILED` when the error is non-retryable *or* this was the last
allowed attempt (`self.request.retries >= self.max_retries` in the
Celery task). Otherwise the run stays `RUNNING` with the latest error
info recorded (`error_category`/`error_message`/`error_is_retryable`) for
visibility, and the Celery task calls `self.retry()` with bounded
exponential backoff + jitter (`apps.execution.retry_policy`,
`MAX_RUN_ATTEMPTS = 5` total tries).

### Resumable extraction

`PipelineRun.last_successful_cursor` and `pages_extracted` are updated
after every successfully processed page. A retried attempt resumes
`source.fetch(credential, cursor=run.last_successful_cursor)` and
continues raw-payload sequence numbering from `run.pages_extracted`,
rather than re-extracting pages that already succeeded.

### At-least-once, not exactly-once -- the duplicate window is explicit

`SqlServerDestinationConnector.load()` is a plain, non-idempotent INSERT.
If a worker crashes *after* a destination transaction commits but
*before* the platform records `pages_extracted`/`records_loaded` for that
page, a retry will re-extract and re-load that page's records --
duplicating them at the destination. This is a real, currently
unmitigated risk, not a hidden one:

- Raw payload storage and metadata *are* idempotent (deterministic
  object keys, `update_or_create` on `(run, sequence)`), so re-running a
  page never corrupts the audit trail.
- The destination load itself is not: there is no upsert, no
  destination-side idempotency key, no distributed transaction linking
  the destination commit to the platform's own state update. Building
  real exactly-once delivery would require either destination-side
  upsert semantics (a real, larger feature -- see the roadmap) or a
  two-phase commit the architecture explicitly avoids (ADR 0001).

Operators integrating with a destination where duplicate rows are costly
should add their own uniqueness constraint or dedupe step downstream
until upsert support lands.

### Redaction is defense in depth, not the primary control

The primary control is discipline: no code path in `apps.execution`,
`apps.connectors`, or `apps.authproviders` puts credential material into
an exception message, log call, or `AuditLog.metadata`.
`apps.core.redaction.sanitize_error_message` is a belt-and-suspenders
regex-based redaction pass applied to every error message before it's
persisted onto `PipelineRun.error_message` / `TaskExecution.error_message`,
for the case where an upstream library (an HTTP client, a DB driver)
unexpectedly echoes something sensitive back in its own exception text.
It also truncates to a bounded length so a pathological error message
can't grow a row without limit.

## Consequences

- Execution history (`PipelineRun`/`TaskExecution`) is the system of
  record for what happened -- not Celery's own task/result state, which
  this platform doesn't rely on for anything beyond triggering retries.
- Operators can trust that a `SUCCEEDED` run really did process every
  page, and that a `FAILED` run's `error_is_retryable` flag reflects
  whether retrying (a fresh manual trigger) is worth attempting again.
- The stuck-`RUNNING`-after-a-hard-crash gap and the at-least-once load
  semantics are the two biggest known correctness caveats of this
  milestone. Both are documented here and in `docs/architecture.md`
  rather than glossed over, and both are natural candidates for a future
  milestone (a staleness reaper; destination upsert support).
