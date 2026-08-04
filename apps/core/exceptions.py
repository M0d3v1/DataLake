class DataLakeError(Exception):
    """Base class for domain errors raised by platform code."""


class ConnectionTestFailed(DataLakeError):
    """A source/destination connection failed its connectivity test."""


class LoadFailed(DataLakeError):
    """A destination connector failed while writing records."""


class FetchFailed(DataLakeError):
    """A source connector failed while fetching records."""


class AuthenticationError(DataLakeError):
    """An AuthProvider could not produce valid credentials for a request."""


class SecretNotFound(DataLakeError):
    """A SecretStore lookup found no value for the given reference."""


class UnsupportedCapability(DataLakeError):
    """A connector was asked to do something it declares it can't do.

    e.g. requesting an incremental fetch from a connector whose
    `capabilities.supports_incremental` is False.
    """
