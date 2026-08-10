"""Orchestrator Agent -- the only decision-making agent in the system.

Per the report (Section 3.1 / 3.3): "Only the Orchestrator makes decisions. It
decides what needs fetching, when, and what to do with the result. It is the
only agent that reads or writes the local state store." This class is
therefore the only thing in the codebase that imports `sync_store`, and it is
the only thing that decides create / update / unchanged / archive.

It absorbs the "Monitor Agent" responsibility the report explicitly considered
and rejected as a separate agent (Section 3, Conclusion): scheduling checks
and comparing results against known state live here, not in a fifth agent,
because both already require read access to the state store this class owns.

Each instance is scoped to one source_system (default "pieas", for backward
compatibility -- every existing call site keeps working unmodified). This
means running a sync against a second source system is just constructing a
second OrchestratorAgent with a different source_system, sharing the same
underlying sync_mapping/sync_state tables without colliding: each
source_system gets its own mapping rows and its own watermark, per
entity_type -- see sync_store.py's docstring.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openedu_orchestrator.config import SYNC_STORE_DB_PATH
from openedu_orchestrator import sync_store as store


class OrchestratorAgent:
    def __init__(self, db_path: Path = SYNC_STORE_DB_PATH, source_system: str = "pieas"):
        self._conn = store.get_connection(db_path)
        store.init_schema(self._conn)
        self._source_system = source_system

    def close(self) -> None:
        self._conn.close()

    # --- mode decision -------------------------------------------------

    def is_bulk_mode(self, entity_type: str) -> bool:
        return store.is_bulk_mode(self._conn, self._source_system, entity_type)

    def get_watermark(self, entity_type: str) -> datetime | None:
        return store.get_watermark(self._conn, self._source_system, entity_type)

    # --- instructions to the Extractor (Section 3.3, Listing 1/2) -------

    def plan_bulk_page(self, entity_type: str, offset: int, page_size: int) -> dict:
        return {"op": "fetch_page", "entity_type": entity_type, "limit": page_size, "offset": offset}

    def plan_change(self, entity_type: str) -> dict:
        return {"op": "fetch_changed", "entity_type": entity_type, "watermark": self.get_watermark(entity_type)}

    def plan_deletion(self, entity_type: str) -> dict:
        return {"op": "fetch_ids", "entity_type": entity_type}

    # --- classification (Section 6.2 / Listing 5) ------------------------

    def classify_records(self, entity_type: str, records: list[dict]) -> list[dict]:
        """Decide create / update / unchanged for each fetched source row
        by looking up sync_mapping -- exactly the upsert-decision lookup in
        Listing 5, plus a content-hash comparison to catch touched-but-
        unchanged rows (e.g. a re-save with identical values).

        Reads record["source_id"] -- every source adapter's fetch functions
        alias their own primary key to that name, regardless of what the
        source's own natural key is called (pieas_id, student_ref, ...),
        so this method has no source-specific assumption left in it.
        """
        classified = []
        for record in records:
            source_id = record["source_id"]
            business_fields = {k: v for k, v in record.items() if k != "last_updated"}
            h = store.content_hash(business_fields)
            mapping = store.get_mapping(self._conn, self._source_system, source_id, entity_type)
            if mapping is None:
                action = "create"
                openeducat_id = None
            elif mapping["content_hash"] == h:
                action = "unchanged"
                openeducat_id = mapping["openeducat_id"]
            else:
                action = "update"
                openeducat_id = mapping["openeducat_id"]
            classified.append({
                "entity_type": entity_type,
                "source_id": source_id,
                "action": action,
                "source_record": record,
                "openeducat_id": openeducat_id,
                "content_hash": h,
            })
        return classified

    # --- deletion classification (Section 5.3, Listing 3/9) ---------------

    def classify_deletions(self, entity_type: str, fetched_ids: list[str]) -> list[dict]:
        """LEFT JOIN semantics without a live cross-database join: sync_mapping
        is local to the Orchestrator, so the comparison is a set difference
        against the ID list the Extractor just fetched from the source.
        """
        fetched_set = set(fetched_ids)
        mappings = store.get_all_mappings(self._conn, self._source_system, entity_type)
        return [
            {"entity_type": entity_type, "source_id": m["source_id"], "openeducat_id": m["openeducat_id"]}
            for m in mappings
            if m["source_id"] not in fetched_set
        ]

    # --- recording outcomes (Section 6.3) ---------------------------------

    def record_load_results(self, entity_type: str, classified: list[dict], load_results: list[dict]) -> None:
        by_source_id = {c["source_id"]: c for c in classified}
        for result in load_results:
            classified_rec = by_source_id.get(result["source_id"])
            if classified_rec is None or not result["ok"]:
                continue
            store.upsert_mapping(
                self._conn,
                source_system=self._source_system,
                source_id=result["source_id"],
                openeducat_id=result["openeducat_id"],
                entity_type=entity_type,
                hash_=classified_rec["content_hash"],
            )

    def record_archive_results(self, entity_type: str, archive_results: list[dict]) -> None:
        for result in archive_results:
            if result["ok"]:
                store.touch_mapping(self._conn, self._source_system, result["source_id"], entity_type)

    def advance_watermark(self, entity_type: str, processed_records: list[dict]) -> None:
        """Advance to the max last_updated actually processed in this run,
        rather than wall-clock NOW(): safer against a row changing again
        mid-run than the report's literal Listing 7, while keeping the same
        watermark mechanism.
        """
        if not processed_records:
            return
        max_ts = max(datetime.fromisoformat(r["last_updated"]) for r in processed_records)
        store.advance_watermark(self._conn, self._source_system, entity_type, max_ts)

    def set_watermark(self, entity_type: str, timestamp: datetime) -> None:
        """Set the watermark directly to a caller-computed timestamp (used by
        the graph's finalize_node, which tracks a running max across
        paginated bulk-migration pages itself).
        """
        store.advance_watermark(self._conn, self._source_system, entity_type, timestamp)

    # --- introspection for CLI / tests -------------------------------------

    def mapping_count(self, entity_type: str) -> int:
        return store.count_mappings(self._conn, self._source_system, entity_type)

    def all_mappings(self, entity_type: str):
        """Every (source_id, openeducat_id, content_hash) this source_system has
        ever synced for entity_type -- used by the reconcile command's drift
        audit, which needs the full mapping set rather than a single lookup.
        """
        return store.get_all_mappings(self._conn, self._source_system, entity_type)
