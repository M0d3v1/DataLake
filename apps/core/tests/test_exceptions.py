from apps.core.exceptions import (
    ConfigurationError,
    DataLakeError,
    FetchFailed,
    MalformedResponseError,
    MappingError,
    SchemaMismatchError,
)


def test_default_retryable_is_false():
    exc = DataLakeError("boom")
    assert exc.retryable is False
    assert exc.category == "DataLakeError"


def test_retryable_and_category_settable_at_raise_site():
    exc = FetchFailed("timeout", retryable=True, category="Timeout")
    assert exc.retryable is True
    assert exc.category == "Timeout"


def test_non_retryable_subclasses_always_non_retryable():
    for exc in (
        ConfigurationError("bad config"),
        MalformedResponseError("bad json"),
        MappingError("bad mapping"),
        SchemaMismatchError("bad schema"),
    ):
        assert exc.retryable is False


def test_malformed_response_error_is_a_fetch_failed():
    assert isinstance(MalformedResponseError("x"), FetchFailed)
