"""Bounded exponential backoff with jitter for retrying a PipelineRun's
Celery task after a retryable failure.

`MAX_RUN_ATTEMPTS` is the total number of tries allowed (the first try
plus retries) -- the Celery task's `max_retries` is one less than this.
"""

import random

MAX_RUN_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 10
MAX_BACKOFF_SECONDS = 300
JITTER_FRACTION = 0.25


def compute_backoff_seconds(retry_number: int) -> float:
    """`retry_number` is Celery's `self.request.retries` (0 on the first
    retry). Exponential growth capped at `MAX_BACKOFF_SECONDS`, plus up to
    `JITTER_FRACTION` extra so many failing runs don't all retry in
    lockstep and thunder the source/destination at the same instant."""
    exponential = min(BASE_BACKOFF_SECONDS * (2**retry_number), MAX_BACKOFF_SECONDS)
    jitter = random.uniform(0, exponential * JITTER_FRACTION)
    return exponential + jitter
