"""Safe dotted-path lookups into plain dict/list JSON structures.

Used by the REST source connector (to pull a nested records list out of a
response) and the destination mapper (to pull a nested source field out of
a record). Deliberately just dict/list indexing -- no `eval`, no
attribute access, no expression language. A path like "data.items" is
just ["data", "items"] walked one key at a time.
"""

from typing import Any


def get_by_path(obj: Any, path: str, *, sep: str = ".") -> tuple[Any, bool]:
    """Return `(value, found)`. `found` is False if any segment of `path`
    is missing (or `obj` stops being a dict along the way) -- callers use
    that to distinguish "missing" from "present but None"."""
    current = obj
    for part in path.split(sep):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True
