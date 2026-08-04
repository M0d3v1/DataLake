from apps.core.redaction import sanitize_error_message


def test_redacts_authorization_header_value():
    message = "request failed, headers: Authorization: Bearer abc123.def456"
    redacted = sanitize_error_message(message)
    assert "abc123" not in redacted
    assert "def456" not in redacted
    assert "[redacted]" in redacted


def test_redacts_bearer_token_anywhere_in_message():
    redacted = sanitize_error_message("token acquisition failed for bearer sk_live_abcdef")
    assert "sk_live_abcdef" not in redacted


def test_redacts_password_like_fields():
    redacted = sanitize_error_message('connection failed: password="hunter2secret"')
    assert "hunter2secret" not in redacted


def test_redacts_api_key_like_fields():
    redacted = sanitize_error_message("api_key: super-secret-value rejected")
    assert "super-secret-value" not in redacted


def test_leaves_ordinary_messages_untouched():
    message = "destination table dbo.policies has no column(s) ['extra_field']"
    assert sanitize_error_message(message) == message


def test_truncates_long_messages():
    message = "x" * 5000
    redacted = sanitize_error_message(message, max_length=100)
    assert len(redacted) <= 120
    assert redacted.endswith("[truncated]")
