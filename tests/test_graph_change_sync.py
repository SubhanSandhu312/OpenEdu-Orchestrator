from __future__ import annotations

from datetime import datetime, timezone

from openedu_orchestrator import pieas_source as src
from openedu_orchestrator.graph import run_cycle
from openedu_orchestrator.models import PieasStudent


def test_change_cycle_picks_up_edit_and_new_admission(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)

    ids = src.fetch_ids(dbs.pieas_conn, "students")
    edited_id = ids[0]
    src.update_fields(dbs.pieas_conn, "students", edited_id, {"department": "Physics"})
    src.insert_student(dbs.pieas_conn, PieasStudent(
        pieas_id="PIEAS-STU-99999", roll_number="2026-CS-999", first_name="New",
        last_name="Admit", email="new.admit@example.com", gender="male",
        date_of_birth="2005-01-01", department="Computer Science", batch_year=2026,
        last_updated=datetime.now(timezone.utc),
    ))

    report = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)

    assert report.fetched == 2
    assert report.created == 1
    assert report.updated == 1
    assert agents.orchestrator.mapping_count("student") == 13

    mapping = agents.orchestrator._conn.execute(
        "SELECT openeducat_id FROM sync_mapping WHERE source_id = ?", (edited_id,)
    ).fetchone()
    updated_row = dbs.oc_client.read("op.student", [mapping["openeducat_id"]])[0]
    assert updated_row["department"] == "Physics"


def test_change_cycle_is_a_noop_when_nothing_changed(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    first = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    # first change cycle after bulk: watermark was already seeded by bulk, so
    # nothing new should be picked up.
    assert first.fetched == 0
    assert first.created == 0
    assert first.updated == 0

    second = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert second.fetched == 0


def test_watermark_only_advances_forward(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    wm1 = agents.orchestrator.get_watermark("student")

    ids = src.fetch_ids(dbs.pieas_conn, "students")
    src.update_fields(dbs.pieas_conn, "students", ids[0], {"department": "Physics"})
    run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    wm2 = agents.orchestrator.get_watermark("student")

    assert wm2 >= wm1


def test_change_cycle_does_not_affect_other_entity_types(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    run_cycle("bulk", "faculty", agents.orchestrator, agents.extractor, agents.loader)

    ids = src.fetch_ids(dbs.pieas_conn, "students")
    src.update_fields(dbs.pieas_conn, "students", ids[0], {"department": "Physics"})

    run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    # faculty watermark/mapping untouched by a student-only change cycle
    assert agents.orchestrator.mapping_count("faculty") == 5
