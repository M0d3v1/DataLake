"""Bounded backoff with jitter for `poll_spark_transform_task` -- the same
shape as `apps.execution.retry_policy`, but tuned for patiently polling a
long-running external job rather than quickly retrying a failed run: many
more attempts, a gentler (1.5x, not 2x) growth rate, and a longer cap.
"""

import random

MAX_POLL_ATTEMPTS = 120
BASE_POLL_BACKOFF_SECONDS = 15
MAX_POLL_BACKOFF_SECONDS = 300
POLL_JITTER_FRACTION = 0.1


def compute_poll_backoff_seconds(retry_number: int) -> float:
    exponential = min(BASE_POLL_BACKOFF_SECONDS * (1.5**retry_number), MAX_POLL_BACKOFF_SECONDS)
    jitter = random.uniform(0, exponential * POLL_JITTER_FRACTION)
    return exponential + jitter
