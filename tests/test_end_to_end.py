"""Full-lifecycle test mirroring the CLI `demo` command: bulk migration,
simulated ongoing PIEAS activity, change cycle, deletion cycle, and a repeat
pass to prove idempotency -- across all three entity types, exactly the
scenario the report's Section 4 and Figure 3 describe.
"""

from __future__ import annotations

from datetime import datetime, timezone

from openedu_orchestrator import pieas_source as src
from openedu_orchestrator.graph import run_cycle
from openedu_orchestrator.models import PieasStudent

ENTITY_TYPES = ("student", "faculty", "course")


def test_full_lifecycle_across_all_entity_types(dbs, agents):
    # 1. Bulk migration: state store starts empty for every entity type.
    for et in ENTITY_TYPES:
        assert agents.orchestrator.is_bulk_mode(et)
        report = run_cycle("bulk", et, agents.orchestrator, agents.extractor, agents.loader, agents.validator)
        assert report.errors == []
        assert report.validation_issues == []

    assert agents.orchestrator.mapping_count("student") == 12
    assert agents.orchestrator.mapping_count("faculty") == 5
    assert agents.orchestrator.mapping_count("course") == 4

    # 2. PIEAS keeps being used: an edit, a new admission, a removal.
    student_ids = src.fetch_ids(dbs.pieas_conn, "students")
    edited, removed = student_ids[0], student_ids[1]
    src.update_fields(dbs.pieas_conn, "students", edited, {"department": "Physics"})
    src.delete_row(dbs.pieas_conn, "students", removed)
    src.insert_student(dbs.pieas_conn, PieasStudent(
        pieas_id="PIEAS-STU-99999", roll_number="2026-CS-999", first_name="New",
        last_name="Admit", email="new.admit@example.com", gender="male",
        date_of_birth="2005-01-01", department="Computer Science", batch_year=2026,
        last_updated=datetime.now(timezone.utc),
    ))

    # 3. Change cycle: catches the edit and the new admission, not the deletion.
    change_report = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader, agents.validator)
    assert change_report.created == 1
    assert change_report.updated == 1
    assert change_report.fetched == 2  # the deletion is invisible here

    # 4. Deletion cycle: catches exactly the removed record.
    deletion_report = run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader, agents.validator)
    assert deletion_report.archived == 1
    assert deletion_report.fetched == 12  # 12 seeded - 1 removed + 1 admitted

    # 5. Re-running both cycles with no further PIEAS activity changes nothing
    #    new (idempotency) -- except the already-archived record, which has
    #    no way to be distinguished from "still missing" by this design and
    #    is harmlessly re-archived (see README's known-characteristics note).
    change_report_2 = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert change_report_2.created == 0
    assert change_report_2.updated == 0

    deletion_report_2 = run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert deletion_report_2.archived == 1  # re-archives the same, already-inactive record

    active_students = dbs.oc_client.search_read("op.student", [("active", "=", True)])
    assert len(active_students) == 12  # 12 seeded - 1 archived + 1 admitted

    # Other entity types were completely unaffected by the student-only cycles.
    assert agents.orchestrator.mapping_count("faculty") == 5
    assert agents.orchestrator.mapping_count("course") == 4
    assert len(dbs.oc_client.search_read("op.faculty", [("active", "=", True)])) == 5
    assert len(dbs.oc_client.search_read("op.course", [("active", "=", True)])) == 4
