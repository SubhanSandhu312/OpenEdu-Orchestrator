"""LangGraph state schema shared by every node in the sync pipeline graph.

One graph definition serves all three cycles described in the report (bulk
migration, change cycle, deletion cycle) -- `mode` is what a node branches on,
not three separate graphs, because all three share the same
Extractor -> Transformer -> Loader pipeline and differ only in what the
Orchestrator asks for and does with the result.
"""

from __future__ import annotations

from typing import Optional, TypedDict

from openedu_orchestrator.models import EntityType, SyncMode


class PipelineState(TypedDict, total=False):
    mode: SyncMode
    entity_type: EntityType

    # bulk pagination cursor
    page_offset: int
    page_size: int
    more_pages: bool
    pages_done: int

    # what the Orchestrator told the Extractor to do this step
    instruction: dict

    # Extractor output: raw PIEAS rows (bulk/change) or bare id list (deletion)
    fetched_records: list[dict]
    fetched_ids: list[str]

    # Orchestrator classification output
    classified: list[dict]        # ClassifiedRecord.model_dump() for create/update
    deletion_candidates: list[dict]  # {entity_type, source_id, openeducat_id}

    # Transformer output: same order as `classified`, OpenEduCat-shaped dicts
    transformed: list[dict]

    # Loader output
    load_results: list[dict]
    archive_results: list[dict]

    # Optional Validation Agent output
    validation_issues: list[str]

    # Run bookkeeping. These are running totals across bulk-migration pages
    # (see graph.py's finalize_node): plain dict keys in LangGraph state are
    # last-write-wins, so a per-page count must be added onto a carried total
    # rather than replacing it, or only the final page's numbers would survive.
    report: dict
    errors: list[str]
    total_fetched: int
    total_created: int
    total_updated: int
    total_unchanged: int
    total_archived: int
    watermark_running_max: Optional[str]
