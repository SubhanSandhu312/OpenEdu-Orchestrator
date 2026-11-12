"""The Orchestrator: phase decisions, ghost detection, and when the watermark moves."""
import config
import state
from agents import Orchestrator
from conftest import FakePieas


def empty_source():
    tables = {table: [] for table, _ in config.ENTITIES}
    return FakePieas(tables, schemas={})


def test_ghosts_are_ids_we_remember_that_the_source_no_longer_has():
    for pieas_id in (1, 2, 3):
        state.upsert("students", pieas_id, "op.student", 100 + pieas_id, "h")
    state.mark_archived("students", 3)          # already retired: not a ghost again

    assert Orchestrator.detect_ghosts("students", [1]) == [2]
    assert Orchestrator.detect_ghosts("students", ["1", "2"]) == []


def test_the_watermark_advances_to_the_newest_row_after_a_clean_load():
    rows = [{"id": 1, "last_updated": "2026-08-10 09:00:00"},
            {"id": 2, "last_updated": "2026-08-12 18:30:00"}]

    Orchestrator.finalize("students", "incremental", rows, {"created": 2, "failed": 0})

    assert state.get_watermark("students") == "2026-08-12 18:30:00"


def test_the_watermark_does_not_advance_if_any_record_failed():
    rows = [{"id": 1, "last_updated": "2026-08-10 09:00:00"}]

    Orchestrator.finalize("students", "bulk", rows, {"created": 0, "failed": 1})

    assert state.get_watermark("students") == "1970-01-01 00:00:00"


def test_a_deletion_scan_leaves_the_watermark_alone():
    state.advance_watermark("students", "2026-08-10 09:00:00")

    Orchestrator.finalize("students", "deletions", [{"id": 1}], {"archived": 1})

    assert state.get_watermark("students") == "2026-08-10 09:00:00"


def test_an_empty_state_store_forces_bulk_whatever_the_model_says(llm):
    llm.responder = lambda prompt: [
        {"entity": table, "phase": "incremental", "instruction": "changed rows only"}
        for table, _ in config.ENTITIES]

    plan = Orchestrator(empty_source()).build_plan("incremental")

    assert {step["phase"] for step in plan} == {"bulk"}


def test_an_explicitly_requested_phase_overrides_the_model(llm):
    for table, model in config.ENTITIES:
        state.upsert(table, 1, model, 101, "h")           # nothing is empty
    llm.responder = lambda prompt: [
        {"entity": table, "phase": "incremental", "instruction": "changed rows only"}
        for table, _ in config.ENTITIES]

    plan = Orchestrator(empty_source()).build_plan("bulk")

    assert {step["phase"] for step in plan} == {"bulk"}
    assert [step["entity"] for step in plan] == [table for table, _ in config.ENTITIES]


def test_the_plan_falls_back_to_the_deterministic_one_when_the_llm_is_unreachable(llm):
    state.upsert("departments", 1, "op.department", 101, "h")

    plan = {step["entity"]: step for step in Orchestrator(empty_source()).build_plan("incremental")}

    assert plan["departments"]["phase"] == "incremental"
    assert "watermark" in plan["departments"]["instruction"]
    assert plan["students"]["phase"] == "bulk"            # never synced: still bulk


def test_a_deletion_run_is_planned_without_calling_the_llm(llm):
    plan = Orchestrator(empty_source()).build_plan("deletions")

    assert llm.prompts == []
    assert {step["phase"] for step in plan} == {"deletions"}


def test_entities_the_model_forgot_are_filled_in_from_the_default_plan(llm):
    llm.responder = lambda prompt: [{"entity": "departments", "phase": "bulk",
                                     "instruction": "all departments"}]

    plan = Orchestrator(empty_source()).build_plan("bulk")

    assert len(plan) == len(config.ENTITIES)
    assert all(step.get("instruction") for step in plan)
