"""Applies a Pipeline's `destination_mapping` (destination_column ->
source_field) to extracted records.

Deliberately not a transformation language: one dotted source-field path
per destination column, no expressions, no computed values. A record
that needs real transformation is a job for a future, explicitly-scoped
feature, not something to bolt onto this mapper.
"""

from typing import Any

from apps.core.exceptions import MappingError
from apps.core.paths import get_by_path


def map_record(
    record: dict[str, Any], mapping: dict[str, str], *, strict: bool = False
) -> dict[str, Any]:
    """Return a new dict of {destination_column: value}, built from
    `record` per `mapping`. Never mutates `record`.

    - A source field that is present with an explicit `null` value maps
      to `None` (preserved, not dropped).
    - A source field that is entirely missing also maps to `None` in
      permissive mode (`strict=False`, the default); in strict mode it
      raises `MappingError` instead of silently substituting null.
    - `record` must be a JSON object (dict); anything else raises
      `MappingError` rather than being coerced or skipped.
    """
    if not isinstance(record, dict):
        raise MappingError(f"expected an object record, got {type(record).__name__}")

    mapped: dict[str, Any] = {}
    for destination_column, source_field in mapping.items():
        value, found = get_by_path(record, source_field)
        if not found:
            if strict:
                raise MappingError(
                    f"source field {source_field!r} missing for destination column "
                    f"{destination_column!r}"
                )
            value = None
        mapped[destination_column] = value
    return mapped


def map_records(
    records: list[dict[str, Any]], mapping: dict[str, str], *, strict: bool = False
) -> list[dict[str, Any]]:
    return [map_record(record, mapping, strict=strict) for record in records]
