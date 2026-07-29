"""LangGraph wiring for the sync pipeline.

One graph definition implements all three cycles from the report (bulk
migration, change cycle, deletion cycle): `state["mode"]` decides what each
node does, since all three cycles share the same
Extractor -> Transformer -> Loader pipeline (Figure 2) and differ only in the
instruction the Orchestrator gives the Extractor and what it does with the
result (Figure 1, Figure 3).

Node responsibilities map 1:1 onto the report's agents:
  plan_node      -> Orchestrator decides what to fetch (Section 3.3)
  extract_node   -> Extractor Agent (pure fetch, Section 3.1)
  classify_node  -> Orchestrator decides create/update/unchanged/archive
  transform_node -> Transformer Agent (pure mapping, skipped for deletion)
  load_node      -> Loader Agent (writes via the OpenEduCat client)
  validate_node  -> optional Validation Agent (Section 3.4)
  finalize_node  -> Orchestrator records mapping/watermark updates
"""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from openedu_orchestrator.agents.extractor import ExtractorAgent
from openedu_orchestrator.agents.loader import LoaderAgent
from openedu_orchestrator.agents.orchestrator import OrchestratorAgent
from openedu_orchestrator.agents.transformer import TransformerAgent
from openedu_orchestrator.agents.validator import ValidationAgent
from openedu_orchestrator.config import BULK_PAGE_SIZE
from openedu_orchestrator.models import RunReport
from openedu_orchestrator.state import PipelineState


def build_pipeline_graph(
    orchestrator: OrchestratorAgent,
    extractor: ExtractorAgent,
    loader: LoaderAgent,
    validator: ValidationAgent | None = None,
):
    """Build (and compile) the shared sync-pipeline graph.

    `validator` is optional per Section 3.4: passing None drops the
    validate_node from the graph entirely rather than turning it into a
    no-op, so the optional-agent boundary is structural, not a runtime flag.
    """

    def plan_node(state: PipelineState) -> dict:
        mode, entity_type = state["mode"], state["entity_type"]
        if mode == "bulk":
            instruction = orchestrator.plan_bulk_page(
                entity_type, state.get("page_offset", 0), state.get("page_size", BULK_PAGE_SIZE)
            )
        elif mode == "change":
            instruction = orchestrator.plan_change(entity_type)
        elif mode == "deletion":
            instruction = orchestrator.plan_deletion(entity_type)
        else:
            raise ValueError(f"Unknown mode: {mode!r}")
        return {"instruction": instruction}

    def extract_node(state: PipelineState) -> dict:
        result = extractor.fetch(state["instruction"])
        update: dict = {"pages_done": state.get("pages_done", 0) + 1}
        if state["mode"] == "deletion":
            update["fetched_ids"] = result["ids"]
        else:
            records = result["records"]
            update["fetched_records"] = records
            page_size = state.get("page_size", BULK_PAGE_SIZE)
            update["more_pages"] = state["mode"] == "bulk" and len(records) == page_size
        return update

    def classify_node(state: PipelineState) -> dict:
        entity_type = state["entity_type"]
        if state["mode"] == "deletion":
            candidates = orchestrator.classify_deletions(entity_type, state.get("fetched_ids", []))
            return {"deletion_candidates": candidates}
        classified = orchestrator.classify_records(entity_type, state.get("fetched_records", []))
        return {"classified": classified}

    def transform_node(state: PipelineState) -> dict:
        entity_type = state["entity_type"]
        actionable = [c for c in state.get("classified", []) if c["action"] in ("create", "update")]
        transformed = [
            {
                "pieas_id": c["pieas_id"],
                "action": c["action"],
                "openeducat_id": c["openeducat_id"],
                "fields": TransformerAgent.transform(entity_type, c["source_record"]),
            }
            for c in actionable
        ]
        return {"transformed": transformed}

    def load_node(state: PipelineState) -> dict:
        entity_type = state["entity_type"]
        if state["mode"] == "deletion":
            results = [
                loader.apply(entity_type, "archive", {}, cand["pieas_id"], cand["openeducat_id"])
                for cand in state.get("deletion_candidates", [])
            ]
            return {"archive_results": [r.model_dump() for r in results]}
        results = loader.apply_batch(entity_type, state.get("transformed", []))
        return {"load_results": [r.model_dump() for r in results]}

    def validate_node(state: PipelineState) -> dict:
        assert validator is not None
        entity_type = state["entity_type"]
        if state["mode"] == "deletion":
            archived_ids = [r["openeducat_id"] for r in state.get("archive_results", []) if r["ok"]]
            issues = validator.validate_archives(entity_type, archived_ids)
            return {"validation_issues": issues}
        transformed_by_pieas_id = {t["pieas_id"]: t for t in state.get("transformed", [])}
        writes = [
            {"openeducat_id": r["openeducat_id"], "fields": transformed_by_pieas_id[r["pieas_id"]]["fields"]}
            for r in state.get("load_results", [])
            if r["ok"] and r["pieas_id"] in transformed_by_pieas_id
        ]
        issues = validator.validate_batch(entity_type, writes)
        return {"validation_issues": state.get("validation_issues", []) + issues}

    def finalize_node(state: PipelineState) -> dict:
        # NOTE: bulk migration loops plan->extract->...->finalize once per
        # page, and LangGraph state keys are last-write-wins (no reducer),
        # so per-page results (classified/load_results/...) would otherwise
        # be clobbered by the next page. Running totals below are the fix:
        # each pass adds *this page's* counts onto the total carried in state.
        entity_type = state["entity_type"]
        errors = list(state.get("errors", []))
        page_fetched = page_created = page_updated = page_unchanged = page_archived = 0
        watermark_running_max_iso = state.get("watermark_running_max")

        if state["mode"] == "deletion":
            archive_results = state.get("archive_results", [])
            orchestrator.record_archive_results(entity_type, archive_results)
            errors += [r["error"] for r in archive_results if not r["ok"] and r["error"]]
            page_fetched = len(state.get("fetched_ids", []))
            page_archived = sum(1 for r in archive_results if r["ok"])
        else:
            classified = state.get("classified", [])
            load_results = state.get("load_results", [])
            orchestrator.record_load_results(entity_type, classified, load_results)
            errors += [r["error"] for r in load_results if not r["ok"] and r["error"]]
            # Advance the watermark in both bulk and change modes (a small
            # refinement beyond the report's literal Listing 7): letting bulk
            # migration also seed the watermark means the *first* change
            # cycle afterwards doesn't have to refetch and reclassify every
            # migrated row as "unchanged" just to discover the watermark.
            # Safe across pages because it's a running max, not an overwrite.
            fetched_records = state.get("fetched_records", [])
            page_max = (
                max(datetime.fromisoformat(r["last_updated"]) for r in fetched_records)
                if fetched_records else None
            )
            prior_max_iso = state.get("watermark_running_max")
            prior_max = datetime.fromisoformat(prior_max_iso) if prior_max_iso else None
            candidates = [t for t in (page_max, prior_max) if t is not None]
            if candidates:
                new_max = max(candidates)
                orchestrator.set_watermark(entity_type, new_max)
                watermark_running_max_iso = new_max.isoformat()
            page_fetched = len(fetched_records)
            ok_pieas_ids = {r["pieas_id"] for r in load_results if r["ok"]}
            page_created = sum(1 for c in classified if c["action"] == "create" and c["pieas_id"] in ok_pieas_ids)
            page_updated = sum(1 for c in classified if c["action"] == "update" and c["pieas_id"] in ok_pieas_ids)
            page_unchanged = sum(1 for c in classified if c["action"] == "unchanged")

        update: dict = {
            "errors": errors,
            "watermark_running_max": watermark_running_max_iso,
            "total_fetched": state.get("total_fetched", 0) + page_fetched,
            "total_created": state.get("total_created", 0) + page_created,
            "total_updated": state.get("total_updated", 0) + page_updated,
            "total_unchanged": state.get("total_unchanged", 0) + page_unchanged,
            "total_archived": state.get("total_archived", 0) + page_archived,
        }
        if state["mode"] == "bulk" and state.get("more_pages"):
            update["page_offset"] = state.get("page_offset", 0) + state.get("page_size", BULK_PAGE_SIZE)
        return update

    def route_after_classify(state: PipelineState) -> str:
        return "load" if state["mode"] == "deletion" else "transform"

    def route_after_finalize(state: PipelineState) -> str:
        if state["mode"] == "bulk" and state.get("more_pages"):
            return "plan"
        return "__end__"

    graph = StateGraph(PipelineState)
    graph.add_node("plan", plan_node)
    graph.add_node("extract", extract_node)
    graph.add_node("classify", classify_node)
    graph.add_node("transform", transform_node)
    graph.add_node("load", load_node)
    graph.add_node("finalize", finalize_node)
    if validator is not None:
        graph.add_node("validate", validate_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "extract")
    graph.add_edge("extract", "classify")
    graph.add_conditional_edges("classify", route_after_classify, {"transform": "transform", "load": "load"})
    graph.add_edge("transform", "load")
    if validator is not None:
        graph.add_edge("load", "validate")
        graph.add_edge("validate", "finalize")
    else:
        graph.add_edge("load", "finalize")
    graph.add_conditional_edges("finalize", route_after_finalize, {"plan": "plan", "__end__": END})

    return graph.compile()


def run_cycle(
    mode: str,
    entity_type: str,
    orchestrator: OrchestratorAgent,
    extractor: ExtractorAgent,
    loader: LoaderAgent,
    validator: ValidationAgent | None = None,
    page_size: int = BULK_PAGE_SIZE,
) -> RunReport:
    """Run one full cycle (bulk / change / deletion) for one entity type and
    return a RunReport summarising what happened. This is the function the
    CLI and the tests call -- it hides the LangGraph state dict from callers.
    """
    started_at = datetime.now(timezone.utc)
    watermark_before = orchestrator.get_watermark(entity_type)

    app = build_pipeline_graph(orchestrator, extractor, loader, validator)
    initial_state: PipelineState = {
        "mode": mode,
        "entity_type": entity_type,
        "page_offset": 0,
        "page_size": page_size,
        "more_pages": False,
        "pages_done": 0,
        "errors": [],
    }
    final_state = app.invoke(initial_state, config={"recursion_limit": 500})

    report = RunReport(
        mode=mode,
        entity_type=entity_type,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        errors=final_state.get("errors", []),
        validation_issues=final_state.get("validation_issues", []),
        watermark_before=watermark_before,
        watermark_after=orchestrator.get_watermark(entity_type),
        pages=final_state.get("pages_done", 0),
    )

    report.fetched = final_state.get("total_fetched", 0)
    report.created = final_state.get("total_created", 0)
    report.updated = final_state.get("total_updated", 0)
    report.unchanged = final_state.get("total_unchanged", 0)
    report.archived = final_state.get("total_archived", 0)

    return report
