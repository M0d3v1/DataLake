"""Raw payload storage abstraction.

Every extract writes the untouched response bytes here *before* any
parsing/mapping happens, so a pipeline run can always be replayed or
audited from exactly what the source returned. The default backend talks
to any S3-compatible store (MinIO locally); swapping to AWS S3 or another
provider means implementing this interface, not changing calling code.
"""

from abc import ABC, abstractmethod

from django.conf import settings
from django.utils.module_loading import import_string


class RawPayloadStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store `data` under `key`, return a URI identifying it."""

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """Return the bytes previously stored at `uri`."""


_store_instance: RawPayloadStore | None = None


def get_raw_payload_store() -> RawPayloadStore:
    global _store_instance
    if _store_instance is None:
        backend_cls = import_string(settings.RAW_STORE_BACKEND)
        _store_instance = backend_cls()
    return _store_instance
