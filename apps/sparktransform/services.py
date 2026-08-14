"""Application services for Spark-mode pipeline runs -- submitting a
transform job once extraction finishes, and finalizing the run once
`apps.sparktransform.tasks.poll_spark_transform_task` sees a terminal job
status. See docs/decisions/0009-spark-backed-transform-mode.md.

Kept separate from `apps.execution.orchestration` the same way that
module is kept separate from `apps.execution.tasks`: this is plain,
directly-testable Python, not Celery-task code.
"""

import json
import re
from typing import Any

import boto3
from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.connectors.base import DestinationConnector
from apps.connectors.registry import get_destination_connector
from apps.core.exceptions import ConfigurationError
from apps.core.logging import get_logger
from apps.execution.claims import mark_run_failed, mark_run_succeeded
from apps.execution.models import PipelineRun
from apps.rawstore.models import RawPayloadRecord
from apps.sparktransform.backends.base import SparkJobStatus, get_spark_job_backend
from apps.sparktransform.expr import (
    compile_to_spark_sql,
    parse_transform_expr,
    validate_source_field,
    validate_transform_expr,
)
from apps.sparktransform.models import SparkTransformConfig

log = get_logger(__name__)

_DEST_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_spark_transform_config(config: SparkTransformConfig) -> None:
    """Validate `config.column_mappings`' shape and grammar.

    Not wired to an automatic Django save-time hook -- this codebase has
    no pipeline-config form/API yet for that hook to attach to (pipelines
    are created via shell/admin today; see
    `apps.sparktransform.models.SparkTransformConfig`). Called explicitly
    by `submit_spark_transform` below as defense in depth, and is the
    function any future pipeline-config UI/API must call before allowing
    a save into Spark mode -- so a bad `transform_expr` is a
    `ConfigurationError`/`TransformExpressionError` there, not a runtime
    surprise days later when the pipeline happens to run.
    """
    if not config.column_mappings:
        raise ConfigurationError(
            f"pipeline {config.pipeline_id}: Spark mode requires at least one column mapping "
            "in SparkTransformConfig.column_mappings"
        )
    for destination_column, column_config in config.column_mappings.items():
        if not _DEST_COLUMN_RE.match(destination_column):
            raise ConfigurationError(
                f"pipeline {config.pipeline_id}: destination column {destination_column!r} "
                "is not a valid SQL-safe identifier"
            )
        if not isinstance(column_config, dict) or "source_field" not in column_config:
            raise ConfigurationError(
                f"pipeline {config.pipeline_id}: column {destination_column!r} must be an "
                "object with at least a 'source_field' key"
            )
        validate_source_field(column_config["source_field"])
        transform_expr = column_config.get("transform_expr")
        if transform_expr is not None:
            validate_transform_expr(transform_expr)


def validate_destination_supports_spark_mode(destination: DestinationConnector) -> None:
    if not destination.capabilities.supports_bulk_load:
        raise ConfigurationError(
            f"{type(destination).__name__} does not support bulk load, which Spark mode "
            "requires -- see DestinationConnector.bulk_load"
        )


def submit_spark_transform(run: PipelineRun) -> None:
    """Submit `run`'s pipeline's Spark transform job. Called by
    `apps.execution.orchestration` once extraction has fully finished
    (every page fetched, raw-stored, and its raw payload immutably
    persisted) for a Spark-mode pipeline, instead of the plain-mode
    in-process map/load. Leaves `run` RUNNING with
    `phase=TRANSFORMING` -- does not finalize it. Finalization happens in
    `finalize_spark_transform` once `poll_spark_transform_task` observes
    a terminal job status.
    """
    pipeline = run.pipeline
    try:
        config = pipeline.spark_transform_config
    except SparkTransformConfig.DoesNotExist as exc:
        raise ConfigurationError(
            f"pipeline {pipeline.id} has processing_mode=spark but no SparkTransformConfig"
        ) from exc
    validate_spark_transform_config(config)

    destination = get_destination_connector(
        pipeline.destination_connection.connector_type, pipeline.destination_connection.config
    )
    validate_destination_supports_spark_mode(destination)

    tenant_uuid = _current_tenant_uuid()
    # Same key scheme apps.rawstore.service.store_raw_payload writes to
    # (tenant_uuid/pipeline_id/run_id/...) -- this is exactly the prefix
    # ADR 0009 requires: server-side generated from PipelineRun.id, never
    # a path pipeline config can override, so Spark can only ever read
    # what this platform already stored immutably for this run.
    input_prefix = f"s3://{settings.RAW_STORE_BUCKET}/{tenant_uuid}/{pipeline.id}/{run.id}/"
    output_prefix = f"{input_prefix}transformed/"

    job_config = _build_job_config(config.column_mappings)

    backend = get_spark_job_backend()
    job_id = backend.submit_job(
        input_prefix=input_prefix, output_prefix=output_prefix, job_config=job_config
    )

    run.spark_job_id = job_id
    run.spark_job_status = "SUBMITTED"
    run.phase = PipelineRun.Phase.TRANSFORMING
    run.save(update_fields=["spark_job_id", "spark_job_status", "phase", "updated_at"])
    log.info(
        "spark_transform.submitted",
        run_id=str(run.id),
        pipeline_id=str(pipeline.id),
        job_id=job_id,
        input_prefix=input_prefix,
        output_prefix=output_prefix,
    )

    # Deferred import: apps.sparktransform.tasks imports
    # finalize_spark_transform from this same module, so importing the
    # task at this module's top level would be a circular import within
    # the app. Nothing external triggers the first poll (submission
    # happens deep inside run_pipeline_task's own execution, not at a
    # dispatch call site the way apps.execution.dispatch.trigger_manual_run
    # dispatches run_pipeline_task) -- so submission is what starts polling.
    from apps.sparktransform.tasks import poll_spark_transform_task

    poll_spark_transform_task.delay(schema_name=connection.schema_name, run_id=str(run.id))


def finalize_spark_transform(run: PipelineRun, job_status: SparkJobStatus) -> None:
    """Called once `job_status.is_terminal` -- read the job's output
    (via `_load_transformed_output`) and bulk-load it through the
    destination connector when the job succeeded; mark the run failed
    with the backend's own error detail otherwise. This is the only
    place a Spark-mode run is finalized (mirrors
    `apps.execution.orchestration._record_success`/`_record_failure`,
    which do the same for plain-mode runs, and both ultimately go
    through `apps.execution.claims`, the only place `PipelineRun.status`
    is mutated)."""
    run.spark_job_status = job_status.state
    run.save(update_fields=["spark_job_status", "updated_at"])

    if not job_status.is_success:
        mark_run_failed(
            run,
            category="SparkJobFailedError",
            message=job_status.error_message or f"Spark job {job_status.job_id} failed",
            retryable=False,
        )
        log.error(
            "spark_transform.failed",
            run_id=str(run.id),
            job_id=job_status.job_id,
            state=job_status.state,
        )
        return

    pipeline = run.pipeline
    destination = get_destination_connector(
        pipeline.destination_connection.connector_type, pipeline.destination_connection.config
    )
    destination_credential = _resolve_credential(pipeline.destination_connection)
    try:
        tenant_uuid = _current_tenant_uuid()
        output_prefix = (
            f"s3://{settings.RAW_STORE_BUCKET}/{tenant_uuid}/{pipeline.id}/{run.id}/transformed/"
        )
        records, records_failed = _load_transformed_output(output_prefix)
        records_loaded = destination.bulk_load(destination_credential, records, mode="append")
    finally:
        del destination_credential

    # Only now -- the bulk load actually succeeded -- do this run's raw
    # payload records become "successfully loaded to the destination".
    # See apps.execution.orchestration._extract_only.
    RawPayloadRecord.objects.filter(run=run).update(
        loaded_successfully=True, updated_at=timezone.now()
    )

    run.records_loaded = records_loaded
    run.records_failed = records_failed
    run.save(update_fields=["records_loaded", "records_failed", "updated_at"])
    mark_run_succeeded(run)
    log.info(
        "spark_transform.succeeded",
        run_id=str(run.id),
        job_id=job_status.job_id,
        records_loaded=records_loaded,
        records_failed=records_failed,
    )


def _resolve_credential(connection_obj) -> dict[str, Any]:
    from apps.connections.models import Credential

    try:
        credential_obj = connection_obj.credential
    except Credential.DoesNotExist:
        return {}
    return credential_obj.resolve()


def _current_tenant_uuid() -> str:
    from apps.orgs.models import Organization

    try:
        return str(Organization.objects.get(schema_name=connection.schema_name).tenant_uuid)
    except Organization.DoesNotExist as exc:
        raise ConfigurationError(
            f"no Organization found for schema {connection.schema_name!r}; Spark mode "
            "requires an active tenant schema"
        ) from exc


def _build_job_config(column_mappings: dict[str, dict]) -> dict[str, dict]:
    """Precompile every column's mapping into the payload a submitted
    Spark job trusts and runs directly: a deterministic alias for each
    distinct `source_field` (so the job can extract it once via
    `get_json_object(payload, "$." + source_field)` -- see
    `spark_jobs/transform_job.py`), plus a Spark SQL expression string
    built entirely from whitelisted pieces (see
    `apps.sparktransform.expr.compile_to_spark_sql`). The job itself
    never re-parses tenant-supplied `transform_expr` text."""
    field_aliases: dict[str, str] = {}
    for column_config in column_mappings.values():
        source_field = column_config["source_field"]
        if source_field not in field_aliases:
            field_aliases[source_field] = f"__src_{len(field_aliases)}"

    job_config: dict[str, dict] = {}
    for destination_column, column_config in column_mappings.items():
        source_field = column_config["source_field"]
        transform_expr = column_config.get("transform_expr")
        alias = field_aliases[source_field]
        if transform_expr:
            node = parse_transform_expr(transform_expr)
            spark_sql_expr = compile_to_spark_sql(node, field_aliases)
        else:
            spark_sql_expr = f"`{alias}`"
        job_config[destination_column] = {
            "source_field": source_field,
            "alias": alias,
            "spark_sql_expr": spark_sql_expr,
        }
    return job_config


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.RAW_STORE_ENDPOINT_URL,
        aws_access_key_id=settings.RAW_STORE_ACCESS_KEY,
        aws_secret_access_key=settings.RAW_STORE_SECRET_KEY,
        use_ssl=settings.RAW_STORE_USE_SSL,
    )


def _load_transformed_output(output_prefix: str) -> tuple[list[dict], int]:
    """Read a completed Spark job's output: newline-delimited JSON
    records (already destination-shaped, per `_build_job_config`'s
    aliasing) under `output_prefix`, plus an optional
    `_manifest.json` (`{"records_failed": int}`) the job writes for rows
    it routed to a dead-letter path instead of the main output -- see
    `spark_jobs/transform_job.py`. Returns `(records, records_failed)`.

    Deliberately reads through this platform's own S3/MinIO bucket and
    credentials (the same ones `apps.rawstore` uses), not through
    `apps.rawstore.base.RawPayloadStore` -- this is Spark's *derived*
    output, not the immutable raw-payload audit trail itself, so it
    doesn't go through that interface's content-addressing/non-overwrite
    guarantees (nothing here claims those guarantees for derived data)."""
    if not output_prefix.startswith("s3://"):
        raise ConfigurationError(f"not a valid output prefix: {output_prefix!r}")
    bucket, _, prefix = output_prefix[len("s3://") :].partition("/")

    client = _s3_client()
    records: list[dict] = []
    records_failed = 0

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key.rsplit("/", 1)[-1]
            if filename == "_manifest.json":
                body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                records_failed = json.loads(body).get("records_failed", 0)
            elif filename.startswith(("_", ".")):
                # Spark control/checksum files (_SUCCESS, .crc, ...), not
                # output data -- skip rather than fail on them.
                continue
            else:
                body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                for line in body.decode("utf-8").splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))

    return records, records_failed
