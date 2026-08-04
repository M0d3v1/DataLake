"""Base Django settings, shared by all environments.

Values are sourced from `config.settings._typed.env` (a validated
pydantic-settings object) rather than reading `os.environ` directly here,
so misconfiguration fails fast and typed.
"""

from pathlib import Path

from config.settings._typed import env

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = env.django_secret_key
DEBUG = env.debug
ALLOWED_HOSTS = env.allowed_hosts_list()

# --------------------------------------------------------------------------
# django-tenants: schema-per-tenant multi-tenancy.
#
# SHARED_APPS live in the "public" Postgres schema (one copy, platform-wide:
# the tenant/domain registry itself, and auth since a user must be resolved
# before we know which tenant's schema to use).
#
# TENANT_APPS are migrated into *every* tenant schema separately -- this is
# where all customer-facing domain data (connections, credentials,
# pipelines, runs, raw payload metadata, audit log) lives, physically
# isolated per organization. See docs/decisions/0002-schema-per-tenant.md.
# --------------------------------------------------------------------------
SHARED_APPS = [
    "django_tenants",
    "apps.core",
    "apps.orgs",
    "apps.accounts",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
]

TENANT_APPS = [
    # django-tenants requires contenttypes in both SHARED_APPS and
    # TENANT_APPS so each schema gets its own content type table.
    "django.contrib.contenttypes",
    "apps.secrets",
    "apps.connectors",
    "apps.authproviders",
    "apps.connections",
    "apps.pipelines",
    "apps.execution",
    "apps.rawstore",
    "apps.auditing",
]

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

TENANT_MODEL = "orgs.Organization"
TENANT_DOMAIN_MODEL = "orgs.Domain"

DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)

MIDDLEWARE = [
    "django_tenants.middleware.main.TenantMainMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --------------------------------------------------------------------------
# Metadata database. This is the ONLY Django-managed database. External
# source/destination systems (customer SQL Server / PostgreSQL) are never
# registered here -- they are reached exclusively via SQLAlchemy Core
# engines built at task-execution time. See
# docs/decisions/0004-internal-external-db-separation.md.
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": env.postgres_db,
        "USER": env.postgres_user,
        "PASSWORD": env.postgres_password,
        "HOST": env.postgres_host,
        "PORT": env.postgres_port,
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------
CELERY_BROKER_URL = env.rabbitmq_url
# No result backend: run/task status is tracked in our own PipelineRun /
# TaskExecution models (apps.execution), which is what execution history
# and monitoring will read from -- Celery's result backend isn't the
# system of record here.
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True

# --------------------------------------------------------------------------
# Object storage (MinIO / S3-compatible) for immutable raw payloads.
# --------------------------------------------------------------------------
RAW_STORE_BACKEND = "apps.rawstore.backends.s3.S3RawPayloadStore"
RAW_STORE_ENDPOINT_URL = env.minio_endpoint
RAW_STORE_ACCESS_KEY = env.minio_access_key
RAW_STORE_SECRET_KEY = env.minio_secret_key
RAW_STORE_BUCKET = env.minio_bucket_raw_payloads
RAW_STORE_USE_SSL = env.minio_use_ssl

# --------------------------------------------------------------------------
# Secret store (see apps.secrets). Default backend encrypts credential
# material with this Fernet key before persisting it. This is a
# placeholder-grade implementation -- production deployments should
# provide a SecretStore backed by a real secrets manager instead of
# relying on an application-level symmetric key. See
# docs/decisions/0003-secret-store-abstraction.md.
# --------------------------------------------------------------------------
SECRET_STORE_ENCRYPTION_KEY = env.secret_store_encryption_key
SECRET_STORE_BACKEND = "apps.secrets.backends.encrypted_field.EncryptedFieldSecretStore"

# --------------------------------------------------------------------------
# Outbound HTTP security policy (apps.core.outbound_http). Every request
# this platform makes to a tenant-configured URL -- REST source requests,
# token-endpoint/multi-step authentication, connection tests -- goes
# through this policy. See docs/decisions/0006-outbound-http-security-policy.md.
# --------------------------------------------------------------------------
OUTBOUND_HTTP_ALLOWED_PRIVATE_HOSTS = env.outbound_http_allowed_private_hosts_list()
OUTBOUND_HTTP_ALLOW_INSECURE_HTTP = env.outbound_http_allow_insecure_http
OUTBOUND_HTTP_MAX_RESPONSE_BYTES = env.outbound_http_max_response_bytes

# --------------------------------------------------------------------------
# Structured logging. Django's own framework logs go through the plain
# stdlib console handler below; application/domain code should instead use
# `apps.core.logging.get_logger()`, which is configured (in
# apps.core.apps.CoreConfig.ready) to render key/value events as JSON via
# structlog -- see docs/architecture.md#logging.
# --------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
