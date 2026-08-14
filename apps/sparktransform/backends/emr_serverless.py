"""AWS EMR Serverless `SparkJobBackend`, via boto3's `emr-serverless`
client -- `boto3` is already a hard dependency (apps.rawstore.backends.s3
uses it for S3/MinIO), so this needs no new package.

NOTE, honestly: the `start_job_run`/`get_job_run` parameter names/shapes
below match documented EMR Serverless boto3 API as of this module's
writing, but this was built with no AWS credentials and no live EMR
Serverless application available to submit a real job against -- it has
only been syntax/type-checked (`ruff check`, `python -m py_compile`),
never exercised against a real AWS account. Verify against
https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/emr-serverless.html
and a real `start_job_run` call before relying on this in production --
see docs/decisions/0009-spark-backed-transform-mode.md.
"""

import json

import boto3
import botocore.exceptions
from django.conf import settings

from apps.core.exceptions import ConfigurationError, SparkJobSubmissionError
from apps.sparktransform.backends.base import SparkJobBackend, SparkJobStatus

_TERMINAL_SUCCESS_STATES = frozenset({"SUCCESS"})
_TERMINAL_FAILURE_STATES = frozenset({"FAILED", "CANCELLED"})
_TERMINAL_STATES = _TERMINAL_SUCCESS_STATES | _TERMINAL_FAILURE_STATES


class EmrServerlessBackend(SparkJobBackend):
    def __init__(self) -> None:
        self._application_id = settings.SPARK_EMR_APPLICATION_ID
        self._execution_role_arn = settings.SPARK_EMR_EXECUTION_ROLE_ARN
        self._entry_point_s3_uri = settings.SPARK_JOB_ENTRY_POINT_S3_URI
        if not (self._application_id and self._execution_role_arn and self._entry_point_s3_uri):
            raise ConfigurationError(
                "EmrServerlessBackend requires SPARK_EMR_APPLICATION_ID, "
                "SPARK_EMR_EXECUTION_ROLE_ARN, and SPARK_JOB_ENTRY_POINT_S3_URI "
                "to all be configured"
            )
        self._client = boto3.client("emr-serverless", region_name=settings.SPARK_AWS_REGION)

    def submit_job(self, *, input_prefix: str, output_prefix: str, job_config: dict) -> str:
        try:
            response = self._client.start_job_run(
                applicationId=self._application_id,
                executionRoleArn=self._execution_role_arn,
                jobDriver={
                    "sparkSubmit": {
                        "entryPoint": self._entry_point_s3_uri,
                        "entryPointArguments": [
                            "--input-prefix",
                            input_prefix,
                            "--output-prefix",
                            output_prefix,
                            "--column-mappings",
                            json.dumps(job_config),
                        ],
                    }
                },
            )
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            raise SparkJobSubmissionError(f"failed to submit EMR Serverless job: {exc}") from exc
        return response["jobRunId"]

    def get_job_status(self, job_id: str) -> SparkJobStatus:
        try:
            response = self._client.get_job_run(
                applicationId=self._application_id, jobRunId=job_id
            )
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            raise SparkJobSubmissionError(
                f"failed to poll EMR Serverless job {job_id}: {exc}"
            ) from exc

        job_run = response["jobRun"]
        state = job_run["state"]
        is_failure = state in _TERMINAL_FAILURE_STATES
        return SparkJobStatus(
            job_id=job_id,
            state=state,
            is_terminal=state in _TERMINAL_STATES,
            is_success=state in _TERMINAL_SUCCESS_STATES,
            error_message=job_run.get("stateDetails") if is_failure else None,
        )
