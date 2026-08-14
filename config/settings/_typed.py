"""Typed environment configuration.

Kept separate from Django's settings module so the values are validated
once, at process start, instead of being read ad hoc (and untyped)
throughout the codebase. Django's settings.py files translate this into
the framework's global settings; application code should prefer
``django.conf.settings`` as usual, not import this module directly.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATALAKE_", extra="ignore")

    django_secret_key: str = Field(default="insecure-dev-key-change-me")
    debug: bool = Field(default=False)
    allowed_hosts: str = Field(default="localhost,127.0.0.1")

    postgres_db: str = Field(default="datalake")
    postgres_user: str = Field(default="datalake")
    postgres_password: str = Field(default="datalake")
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)

    rabbitmq_url: str = Field(default="amqp://guest:guest@localhost:5672//")

    minio_endpoint: str = Field(default="http://localhost:9000")
    minio_access_key: str = Field(default="minioadmin")
    minio_secret_key: str = Field(default="minioadmin")
    minio_bucket_raw_payloads: str = Field(default="datalake-raw-payloads")
    minio_use_ssl: bool = Field(default=False)

    # Fernet key used by the default SecretStore implementation. Generate
    # with `python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"`.
    # This is a *placeholder* boundary: production deployments should swap
    # the SecretStore backend for a real secrets manager rather than rely
    # on this key living in the environment.
    secret_store_encryption_key: str = Field(
        default="B4Q8yYib3-h1P8m5S5r2G8f5s0d0f5g6h7j8k9l0m1o="
    )

    # --- Outbound HTTP security policy (apps.core.outbound_http) ---------
    # Deployment-wide allowlist of hosts the outbound policy may reach
    # even though they resolve to a *private* (RFC1918/ULA/documentation)
    # address -- e.g. an internal enterprise system every tenant on this
    # deployment is allowed to integrate with. Comma-separated hostnames.
    # Tenant-specific approvals go through apps.connections.models.AllowedOutboundHost,
    # which grants exactly the same "private" bypass and nothing more.
    # Neither this nor the tenant allowlist ever bypasses loopback,
    # link-local (cloud metadata), multicast, or unspecified -- see
    # outbound_http_allowed_unsafe_hosts below. See
    # docs/decisions/0006-outbound-http-security-policy.md.
    outbound_http_allowed_private_hosts: str = Field(default="")
    # Deployment-only allowlist for the categories deliberately excluded
    # above: loopback, link-local (cloud metadata), multicast, unspecified.
    # There is no tenant-level equivalent -- a tenant-configured allowlist
    # entry must never be able to reach the platform's own loopback
    # interface or a cloud metadata endpoint. Only ever set this for a
    # deployment that genuinely needs it (e.g. a local sidecar/test
    # double reachable at 127.0.0.1 in an isolated dev/CI environment).
    outbound_http_allowed_unsafe_hosts: str = Field(default="")
    # Plain http:// is refused by default. Being on either allowlist
    # above does NOT implicitly permit http:// -- that would silently
    # downgrade transport security for a destination approved only for
    # its *address*, not its scheme. Set outbound_http_allow_insecure_http
    # for a deployment-wide exception, or list specific hosts here for a
    # narrower one (e.g. an isolated internal network without TLS).
    outbound_http_allow_insecure_http: bool = Field(default=False)
    outbound_http_insecure_allowed_hosts: str = Field(default="")
    # Hard deployment ceiling: a connector's own `max_response_bytes`
    # config may set a *lower* cap, never a higher one. See
    # apps.core.outbound_http.resolve_max_response_bytes.
    outbound_http_max_response_bytes: int = Field(default=10 * 1024 * 1024)
    # Hard deployment ceiling on any single timeout phase (connect/read/
    # write/pool) a connector's config may request. A connector's
    # `timeout_seconds` may lower the read timeout, never raise it past
    # this. See apps.core.outbound_http.resolve_timeout_seconds.
    outbound_http_max_timeout_seconds: float = Field(default=120.0)

    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    def outbound_http_allowed_private_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.outbound_http_allowed_private_hosts.split(",") if h.strip()]

    def outbound_http_allowed_unsafe_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.outbound_http_allowed_unsafe_hosts.split(",") if h.strip()]

    def outbound_http_insecure_allowed_hosts_list(self) -> list[str]:
        return [
            h.strip() for h in self.outbound_http_insecure_allowed_hosts.split(",") if h.strip()
        ]

    # --- Internal orchestration API (apps.orchestration_api) -------------
    # Shared secret an external orchestrator (Airflow -- see
    # docs/decisions/0010-airflow-orchestration.md) presents as a Bearer
    # token. Empty by default, which the API treats as "not configured"
    # and refuses every request (503), rather than falling open. Generate
    # with e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    orchestration_api_token: str = Field(default="")

    # --- Spark-backed transform mode (apps.sparktransform) ---------------
    # See docs/decisions/0009-spark-backed-transform-mode.md. Default
    # backend is AWS EMR Serverless; swapping to another serverless Spark
    # platform (Databricks Jobs API, GCP Dataproc Serverless) means
    # pointing this at another apps.sparktransform.backends.base.SparkJobBackend
    # implementation, not changing calling code.
    spark_job_backend: str = Field(
        default="apps.sparktransform.backends.emr_serverless.EmrServerlessBackend"
    )
    spark_aws_region: str = Field(default="us-east-1")
    # The EMR Serverless application (a pre-created, standing Spark
    # runtime configuration) and IAM role the submitted job runs as --
    # both deployment/ops-provisioned, never created by this codebase.
    spark_emr_application_id: str = Field(default="")
    spark_emr_execution_role_arn: str = Field(default="")
    # S3 URI of the Spark job driver script (spark_jobs/transform_job.py
    # in this repo) that EMR Serverless actually runs -- uploaded there
    # as a separate deployment step, not by this codebase at runtime.
    spark_job_entry_point_s3_uri: str = Field(default="")


env = Env()
