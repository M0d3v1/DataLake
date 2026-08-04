"""Structured logging for application/domain code.

Framework logs (Django, Celery internals) go through the plain stdlib
console handler configured in settings. Domain code should instead do::

    from apps.core.logging import get_logger
    log = get_logger(__name__)
    log.info("pipeline_run.started", run_id=str(run.id), pipeline_id=str(pipeline.id))

so execution logs are key/value, JSON-rendered, and easy to correlate by
`run_id` across extract/transform/load steps and across processes
(web/worker/scheduler all use the same configuration).
"""

import structlog


def configure_structlog() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "datalake"):
    return structlog.get_logger(name)
