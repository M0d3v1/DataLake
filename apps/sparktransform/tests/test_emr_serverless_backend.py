from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest
from django.test import override_settings

from apps.core.exceptions import ConfigurationError, SparkJobSubmissionError
from apps.sparktransform.backends.emr_serverless import EmrServerlessBackend

VALID_SETTINGS = {
    "SPARK_EMR_APPLICATION_ID": "app-123",
    "SPARK_EMR_EXECUTION_ROLE_ARN": "arn:aws:iam::123456789012:role/spark-role",
    "SPARK_JOB_ENTRY_POINT_S3_URI": "s3://bucket/transform_job.py",
    "SPARK_AWS_REGION": "us-east-1",
}
_BOTO_CLIENT = "apps.sparktransform.backends.emr_serverless.boto3.client"


@override_settings(**VALID_SETTINGS)
def test_missing_required_settings_raises_configuration_error():
    with override_settings(SPARK_EMR_APPLICATION_ID=""):
        with pytest.raises(ConfigurationError):
            EmrServerlessBackend()


@override_settings(**VALID_SETTINGS)
def test_submit_job_returns_job_run_id():
    mock_client = MagicMock()
    mock_client.start_job_run.return_value = {"jobRunId": "run-abc"}

    with patch(_BOTO_CLIENT, return_value=mock_client):
        backend = EmrServerlessBackend()
        job_id = backend.submit_job(
            input_prefix="s3://bucket/in/", output_prefix="s3://bucket/out/", job_config={}
        )

    assert job_id == "run-abc"
    kwargs = mock_client.start_job_run.call_args.kwargs
    assert kwargs["applicationId"] == "app-123"
    assert kwargs["executionRoleArn"] == VALID_SETTINGS["SPARK_EMR_EXECUTION_ROLE_ARN"]
    assert kwargs["jobDriver"]["sparkSubmit"]["entryPoint"] == "s3://bucket/transform_job.py"
    args = kwargs["jobDriver"]["sparkSubmit"]["entryPointArguments"]
    assert "--input-prefix" in args
    assert "s3://bucket/in/" in args


@override_settings(**VALID_SETTINGS)
def test_submit_job_wraps_boto_errors_as_retryable():
    mock_client = MagicMock()
    mock_client.start_job_run.side_effect = botocore.exceptions.ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartJobRun"
    )

    with patch(_BOTO_CLIENT, return_value=mock_client):
        backend = EmrServerlessBackend()
        with pytest.raises(SparkJobSubmissionError) as exc_info:
            backend.submit_job(
                input_prefix="s3://bucket/in/", output_prefix="s3://bucket/out/", job_config={}
            )

    assert exc_info.value.retryable is True


@override_settings(**VALID_SETTINGS)
def test_get_job_status_terminal_success():
    mock_client = MagicMock()
    mock_client.get_job_run.return_value = {"jobRun": {"state": "SUCCESS"}}

    with patch(_BOTO_CLIENT, return_value=mock_client):
        backend = EmrServerlessBackend()
        status = backend.get_job_status("run-abc")

    assert status.state == "SUCCESS"
    assert status.is_terminal is True
    assert status.is_success is True
    assert status.error_message is None


@override_settings(**VALID_SETTINGS)
def test_get_job_status_terminal_failure_includes_error_message():
    mock_client = MagicMock()
    mock_client.get_job_run.return_value = {
        "jobRun": {"state": "FAILED", "stateDetails": "OOM killed"}
    }

    with patch(_BOTO_CLIENT, return_value=mock_client):
        backend = EmrServerlessBackend()
        status = backend.get_job_status("run-abc")

    assert status.is_terminal is True
    assert status.is_success is False
    assert status.error_message == "OOM killed"


@override_settings(**VALID_SETTINGS)
def test_get_job_status_non_terminal_running():
    mock_client = MagicMock()
    mock_client.get_job_run.return_value = {"jobRun": {"state": "RUNNING"}}

    with patch(_BOTO_CLIENT, return_value=mock_client):
        backend = EmrServerlessBackend()
        status = backend.get_job_status("run-abc")

    assert status.is_terminal is False
    assert status.is_success is False


@override_settings(**VALID_SETTINGS)
def test_get_job_status_wraps_boto_errors_as_retryable():
    mock_client = MagicMock()
    mock_client.get_job_run.side_effect = botocore.exceptions.ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "oops"}}, "GetJobRun"
    )

    with patch(_BOTO_CLIENT, return_value=mock_client):
        backend = EmrServerlessBackend()
        with pytest.raises(SparkJobSubmissionError) as exc_info:
            backend.get_job_status("run-abc")

    assert exc_info.value.retryable is True
