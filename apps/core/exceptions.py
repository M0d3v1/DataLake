class DataLakeError(Exception):
    """Base class for domain errors raised by platform code.

    `retryable` and `category` are read by the execution orchestration
    layer (apps.execution.claims / apps.execution.orchestration) to decide
    whether to retry a failed pipeline run via Celery or fail it
    permanently -- see docs/decisions/0005-execution-orchestration.md.
    Classification happens at the raise site, where the real cause (a
    timeout vs. a 4xx vs. a config error) is known; nothing downstream
    re-guesses it from a message string.
    """

    def __init__(self, message: str, *, retryable: bool = False, category: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.category = category or type(self).__name__


class ConnectionTestFailed(DataLakeError):
    """A source/destination connection failed its connectivity test."""


class LoadFailed(DataLakeError):
    """A destination connector failed while writing records."""


class FetchFailed(DataLakeError):
    """A source connector failed while fetching records."""


class MalformedResponseError(FetchFailed):
    """A source response could not be parsed/understood (bad JSON, a
    missing or wrongly-typed records field, ...). Always non-retryable --
    retrying won't change a response shape the connector doesn't
    understand."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="MalformedResponseError")


class AuthenticationError(DataLakeError):
    """An AuthProvider could not produce valid credentials for a request,
    including after a controlled token-refresh attempt. Always
    non-retryable at this point -- if a refresh could have fixed it, that
    was already tried before this was raised."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="AuthenticationError")


class SecretNotFound(DataLakeError):
    """A SecretStore lookup found no value for the given reference."""


class UnsupportedCapability(DataLakeError):
    """A connector was asked to do something it declares it can't do.

    e.g. requesting an incremental fetch from a connector whose
    `capabilities.supports_incremental` is False.
    """

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="UnsupportedCapability")


class ConfigurationError(DataLakeError):
    """Invalid connector/pipeline/auth-provider configuration. Always
    non-retryable -- retrying with the same bad configuration fails
    identically every time."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="ConfigurationError")


class MappingError(DataLakeError):
    """A record could not be mapped to the destination shape (e.g. a
    non-object record, or a required source field missing in strict
    mode). Always non-retryable."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="MappingError")


class SchemaMismatchError(LoadFailed):
    """The destination table doesn't match the configured mapping (e.g.
    references an unknown column, or the table doesn't exist). Always
    non-retryable."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, category="SchemaMismatchError")
