"""Raw payload storage abstraction.

Every extract writes the untouched response bytes here *before* any
parsing/mapping happens, so a pipeline run can always be replayed or
audited from exactly what the source returned. The default backend talks
to any S3-compatible store (MinIO locally); swapping to AWS S3 or another
provider means implementing this interface, not changing calling code.

Implementations are responsible for the platform's immutability and
tenant-isolation guarantees (see
docs/decisions/0007-raw-payload-immutability.md), not just for moving
bytes: `put()` must not overwrite an object that already exists under the
same key, must scope the key to the currently active tenant regardless of
what the caller passed in, and must never provision storage
infrastructure (buckets/containers) as a side effect of a normal write.
`get()` must refuse to return an object outside the configured storage
location or outside the current tenant.
"""

from abc import ABC, abstractmethod

from django.conf import settings
from django.utils.module_loading import import_string


class RawPayloadStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store `data` under a tenant-scoped version of `key`, and
        return a URI identifying it. `key` should be content-addressed
        (include a checksum of `data`) by the caller so that retrying
        with identical bytes naturally reuses the same object instead of
        writing a duplicate -- see `apps.rawstore.service.store_raw_payload`.
        Never overwrites an existing object."""

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """Return the bytes previously stored at `uri`. Raises
        `apps.core.exceptions.RawPayloadAccessDenied` for a malformed
        URI, a URI outside the configured bucket, or a URI belonging to
        a different tenant than the one currently active."""


_store_instance: RawPayloadStore | None = None


def get_raw_payload_store() -> RawPayloadStore:
    global _store_instance
    if _store_instance is None:
        backend_cls = import_string(settings.RAW_STORE_BACKEND)
        _store_instance = backend_cls()
    return _store_instance
