"""Defense-in-depth redaction for anything that might end up in an
execution record, audit event, or log line.

Nothing in this codebase should deliberately put a secret into an error
message in the first place (see docs/decisions/0005-execution-orchestration.md).
This is the belt-and-suspenders layer for the case where an upstream
library's exception text (an HTTP client, a DB driver) unexpectedly
echoes back something sensitive.
"""

import re

MAX_ERROR_MESSAGE_LENGTH = 1000

_REDACT_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)((?:api[_-]?key|password|secret|token)\s*[\"']?\s*[:=]\s*[\"']?)\S+"),
]


def sanitize_error_message(message: str, *, max_length: int = MAX_ERROR_MESSAGE_LENGTH) -> str:
    redacted = message
    for pattern in _REDACT_PATTERNS:
        redacted = pattern.sub(r"\1[redacted]", redacted)
    if len(redacted) > max_length:
        redacted = redacted[:max_length] + "... [truncated]"
    return redacted
