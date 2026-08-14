"""Standalone PySpark driver for a Spark-mode pipeline run -- see
docs/decisions/0009-spark-backed-transform-mode.md.

Deliberately self-contained: this file imports nothing from `apps/` and
never re-parses tenant-supplied `transform_expr` text. Django (see
`apps.sparktransform.services.submit_spark_transform`) already validated
every column against the whitelisted grammar
(`apps.sparktransform.expr`) and precompiled it into the `--column-mappings`
JSON payload this script receives -- a `{destination_column: {"source_field":
..., "alias": ..., "spark_sql_expr": ...}}` mapping where every
`spark_sql_expr` is already a safe Spark SQL string built entirely from
backtick-quoted aliases, whitelisted function/CAST-type names, and
numeric literals. This script trusts that payload and just runs it; it
never sees the original `transform_expr` strings. This mirrors why
`dags/pipeline_dag_factory.py` (ADR 0010) also never imports this
project's Django code -- an external execution engine talking to this
platform over a narrow, trusted contract, not sharing a Python
environment with it.

Submitted via `apps.sparktransform.backends.emr_serverless.EmrServerlessBackend`
as the `sparkSubmit.entryPoint` (an S3 URI this file is uploaded to as a
separate deployment step -- see `SPARK_JOB_ENTRY_POINT_S3_URI`).

NOTE, honestly: written and only syntax-checked (`ruff check`,
`python -m py_compile`) -- there is no PySpark installation, AWS account,
or live EMR Serverless application available in the environment this was
built in, so it has never actually been submitted to a Spark cluster.
Requires the S3A filesystem connector (bundled with EMR Serverless) to
read/write `s3://` paths -- verify before first use.

Usage:
    spark-submit transform_job.py \\
        --input-prefix s3://bucket/tenant/pipeline/run/ \\
        --output-prefix s3://bucket/tenant/pipeline/run/transformed/ \\
        --column-mappings '{"policy_id": {"source_field": "id", "alias": "__src_0", \\
"spark_sql_expr": "`__src_0`"}}'
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import boto3
from pyspark.sql import Row, SparkSession


def _get_by_path(obj: Any, path: str) -> Any:
    """Same dotted-path semantics as apps.core.paths.get_by_path,
    duplicated here rather than imported -- see the module docstring."""
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _extract_records(raw_json_text: str, records_path: str | None) -> list[dict] | None:
    """Parse one raw payload file's JSON body and return its list of
    records, or None if the file is malformed / doesn't have the
    expected shape (routed to the failed count by the caller) --
    mirrors apps.connectors.sources.rest_api's records_path/records_key
    extraction, applied here per-file instead of per-HTTP-response."""
    try:
        body = json.loads(raw_json_text)
    except (json.JSONDecodeError, ValueError):
        return None

    if records_path:
        records = _get_by_path(body, records_path)
    else:
        records = body

    if isinstance(records, list):
        return [r for r in records if isinstance(r, dict)]
    if isinstance(records, dict):
        return [records]
    return None


def _extract_aliased_row(record: dict, field_aliases: dict[str, str]) -> Row:
    """Extract every distinct source_field this pipeline's column
    mappings reference into its precompiled alias column, as a string
    (transform_expr's CAST(... AS ...) is how a column becomes a
    number/date/bool -- see apps.sparktransform.expr) -- matching
    apps.sparktransform.expr's reference evaluator, which also
    stringifies before applying TRIM/UPPER/LOWER."""
    values = {}
    for source_field, alias in field_aliases.items():
        value = _get_by_path(record, source_field)
        values[alias] = None if value is None else str(value)
    return Row(**values)


def run(
    spark: SparkSession, *, input_prefix: str, output_prefix: str, column_mappings: dict
) -> None:
    field_aliases: dict[str, str] = {}
    for column_config in column_mappings.values():
        field_aliases[column_config["source_field"]] = column_config["alias"]

    records_path = None  # kept simple: whole raw body is the records list/object.

    raw_files = spark.sparkContext.wholeTextFiles(f"{input_prefix}*.raw")

    def to_rows(pair):
        _filename, content = pair
        records = _extract_records(content, records_path)
        if records is None:
            return [("__FAILED__", None)]
        return [("__OK__", _extract_aliased_row(r, field_aliases)) for r in records]

    tagged = raw_files.flatMap(to_rows)
    records_failed = tagged.filter(lambda t: t[0] == "__FAILED__").count()
    rows = tagged.filter(lambda t: t[0] == "__OK__").map(lambda t: t[1])

    alias_columns = sorted(field_aliases.values())
    df = spark.createDataFrame(rows, schema=alias_columns)

    select_exprs = [
        f"{column_config['spark_sql_expr']} AS `{destination_column}`"
        for destination_column, column_config in column_mappings.items()
    ]
    transformed = df.selectExpr(*select_exprs)

    transformed.write.mode("overwrite").json(output_prefix)

    bucket, _, prefix = output_prefix[len("s3://") :].partition("/")
    manifest_key = f"{prefix.rstrip('/')}/_manifest.json"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps({"records_failed": records_failed}).encode("utf-8"),
        ContentType="application/json",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-prefix", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--column-mappings", required=True)
    args = parser.parse_args()

    spark = SparkSession.builder.appName("datalake-spark-transform").getOrCreate()
    try:
        run(
            spark,
            input_prefix=args.input_prefix,
            output_prefix=args.output_prefix,
            column_mappings=json.loads(args.column_mappings),
        )
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
