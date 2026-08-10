"""The formal contract a source-system adapter must satisfy.

Until now, "any source system works" was an unenforced convention:
pieas_source.py and pieas_source_mysql.py happen to expose identical
function signatures, but nothing checked that, and nothing told a new
adapter author what's actually required. This Protocol makes that
contract explicit and type-checkable -- matching the project's existing
philosophy of enforcing boundaries structurally rather than by convention
(the same reasoning behind the three-database separation).

An adapter module (not a class -- these are plain modules with top-level
functions, matching pieas_source.py's existing shape) satisfies this
Protocol if it exposes:

    get_connection(conn_info=None) -> connection
    fetch_changed(conn, table, watermark) -> list[dict]
    fetch_page(conn, table, limit, offset) -> list[dict]
    fetch_ids(conn, table) -> list[str]

That's the read-path contract ExtractorAgent actually needs. Adapters
used for seeding/demo purposes (as both existing ones are) additionally
expose insert_*/update_fields/delete_row/reset_database, but those aren't
part of the pipeline's own contract -- they're test/demo scaffolding, not
something a real production source adapter (which wouldn't let this
system write back to the source system at all) would need to implement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class SourceAdapter(Protocol):
    def get_connection(self, conn_info: Optional[Any] = None) -> Any: ...

    def fetch_changed(self, conn: Any, table: str, watermark: Optional[datetime]) -> list[dict]: ...

    def fetch_page(self, conn: Any, table: str, limit: int, offset: int) -> list[dict]: ...

    def fetch_ids(self, conn: Any, table: str) -> list[str]: ...


def check_adapter(module: Any) -> None:
    """Raise a clear error naming exactly which function is missing,
    rather than letting a new adapter fail confusingly deep inside
    ExtractorAgent the first time a particular method happens to get
    called. Call this once when registering a new source system.
    """
    required = ("get_connection", "fetch_changed", "fetch_page", "fetch_ids")
    missing = [name for name in required if not callable(getattr(module, name, None))]
    if missing:
        raise TypeError(
            f"{module.__name__!r} does not satisfy the SourceAdapter contract -- "
            f"missing: {', '.join(missing)}"
        )
