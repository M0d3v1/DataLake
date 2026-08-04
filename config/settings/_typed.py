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

    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


env = Env()
