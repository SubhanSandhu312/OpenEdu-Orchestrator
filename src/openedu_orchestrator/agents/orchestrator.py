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
    def __init__(
        self,
        db_path: Path = SYNC_STORE_DB_PATH,
        source_system: str = "pieas",
        target: str | None = None,
    ):
        self._conn = store.get_connection(db_path)
        store.init_schema(self._conn)
        self._source_system = source_system
        self._target = target

    def ensure_target(self, entity_type: str) -> None:
        """Refuse to run against a different target than the one that wrote
        the current mappings.

        sync_mapping's openeducat_id values only mean anything in one
        target's id space. Handing a mock id to a real Odoo instance does
        not fail -- it silently updates whatever unrelated record happens to
        hold that id. This is the guard against that, and it is checked
        before any cycle does work rather than left to a caller to remember.

        `target=None` opts out entirely, so every existing caller and test
        that never knew about targets keeps working unchanged.
        """
        if self._target is None:
            return
        recorded = store.get_target(self._conn, self._source_system, entity_type)
        if recorded == self._target:
            return
        if recorded is None:
            # No target recorded. Safe to adopt only if nothing has been
            # synced yet -- an empty store has no ids to misinterpret. If
            # mappings already exist, they came from *some* target and this
            # store predates target tracking, so which one is genuinely
            # unknown and adopting would be a guess with silent-corruption
            # consequences.
            if self.mapping_count(entity_type) == 0:
                store.set_target(self._conn, self._source_system, entity_type, self._target)
                return
            raise store.TargetMismatchError(
                f"{entity_type}: this state store already holds "
                f"{self.mapping_count(entity_type)} mappings but predates target tracking, "
                f"so which target their OpenEduCat ids belong to is unknown. Refusing to "
                f"guess, because guessing wrong silently updates unrelated records.\n"
                f"  - If these came from the mock: run 'seed' to reset everything cleanly.\n"
                f"  - If these came from a real Odoo: delete data/orchestrator_state.db and "
                f"re-run 'migrate --target real' (already-synced records stay discoverable "
                f"via their external IDs, so they are matched rather than duplicated)."
            )
        if recorded != self._target:
            raise store.TargetMismatchError(
                f"{entity_type}: this state store was last synced to target {recorded!r}, "
                f"but you are running against {self._target!r}. The stored OpenEduCat ids "
                f"belong to {recorded!r}'s id space, so continuing would update unrelated "
                f"records in {self._target!r}.\n"
                f"  - To demo against the mock: run 'seed' (resets source, mock target, and "
                f"this store).\n"
                f"  - To go back to {recorded!r}: re-run with --target {recorded}.\n"
                f"  - To repoint at {self._target!r} deliberately: delete "
                f"data/orchestrator_state.db and re-run 'migrate' (records already in the "
                f"target stay discoverable via their external IDs)."
            )

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
            elif mapping["archived"]:
                # Present in the source but archived in the target: the
                # record was deleted, then came back. It must be revived even
                # if its content is byte-identical to before, so this is
                # checked *before* the content-hash shortcut -- otherwise a
                # delete-then-restore-unchanged would hash equal, classify as
                # "unchanged", never be written, and stay invisible in the
                # target forever. Found by reconcile flagging exactly that
                # case as stale_archived with nothing able to repair it.
                action = "update"
                openeducat_id = mapping["openeducat_id"]
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
                # Remember that the target record is now inactive, so that if
                # this source_id ever reappears the change cycle revives it
                # instead of deciding it is unchanged.
                store.mark_archived(self._conn, self._source_system, result["source_id"], entity_type)

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

    def adopt_mapping(self, entity_type: str, source_id: str, openeducat_id: int, record: dict) -> None:
        """Record a mapping for a record already present in the target,
        discovered by external-ID lookup rather than by writing it.

        Used to rebuild a lost or repointed state store. The content hash is
        computed exactly as classify_records would, so the next change cycle
        sees the record as `unchanged` rather than re-writing everything.
        """
        business_fields = {k: v for k, v in record.items() if k != "last_updated"}
        store.upsert_mapping(
            self._conn,
            source_system=self._source_system,
            source_id=source_id,
            openeducat_id=openeducat_id,
            entity_type=entity_type,
            hash_=store.content_hash(business_fields),
        )

    def claim_target(self, entity_type: str) -> None:
        """Record which target this store's ids belong to, after a rebuild
        has established that they really do belong to it.
        """
        if self._target is not None:
            store.set_target(self._conn, self._source_system, entity_type, self._target)

    def all_mappings(self, entity_type: str):
        """Every (source_id, openeducat_id, content_hash) this source_system has
        ever synced for entity_type -- used by the reconcile command's drift
        audit, which needs the full mapping set rather than a single lookup.
        """
        return store.get_all_mappings(self._conn, self._source_system, entity_type)
