"""Extractor Agent -- the only agent that ever talks to PIEAS.

Per the report (Section 3.1): "It is a pure fetcher: given an instruction, it
goes and gets exactly that data, and nothing more. It holds no memory between
calls and makes no decisions about what the data means." Concretely: every
public method here takes an explicit instruction and returns raw data: it
never looks at sync_mapping, never decides create vs. update, and never
retains results from a previous call.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from openedu_orchestrator.config import PIEAS_DB_PATH, PIEAS_TABLE_FOR_ENTITY
from openedu_orchestrator import pieas_source as src


class ExtractorAgent:
    def __init__(self, db_path: Path = PIEAS_DB_PATH):
        self._conn = src.get_connection(db_path)

    def close(self) -> None:
        self._conn.close()

    def fetch_changed(self, entity_type: str, watermark: Optional[datetime]) -> list[dict]:
        """Instruction 1 (Listing 1): rows where last_updated > watermark."""
        table = PIEAS_TABLE_FOR_ENTITY[entity_type]
        rows = src.fetch_changed(self._conn, table, watermark)
        return [dict(row) for row in rows]

    def fetch_page(self, entity_type: str, limit: int, offset: int) -> list[dict]:
        """Bulk-migration instruction: one page of the full table."""
        table = PIEAS_TABLE_FOR_ENTITY[entity_type]
        rows = src.fetch_page(self._conn, table, limit, offset)
        return [dict(row) for row in rows]

    def fetch_ids(self, entity_type: str) -> list[str]:
        """Instruction 2 (Listing 2): full unfiltered ID list, for the deletion cycle."""
        table = PIEAS_TABLE_FOR_ENTITY[entity_type]
        return src.fetch_ids(self._conn, table)

    def fetch(self, instruction: dict) -> dict:
        """Single entry point mirroring 'takes one of two instructions from the
        Orchestrator' -- dispatches on instruction['op'] so graph nodes can call
        the Extractor generically without knowing which cycle triggered it.
        """
        op = instruction["op"]
        entity_type = instruction["entity_type"]
        if op == "fetch_changed":
            records = self.fetch_changed(entity_type, instruction.get("watermark"))
            return {"records": records}
        if op == "fetch_page":
            records = self.fetch_page(entity_type, instruction["limit"], instruction["offset"])
            return {"records": records, "returned": len(records)}
        if op == "fetch_ids":
            return {"ids": self.fetch_ids(entity_type)}
        raise ValueError(f"Unknown extractor instruction op: {op!r}")
