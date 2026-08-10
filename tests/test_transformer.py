from __future__ import annotations

import pytest

from openedu_orchestrator.agents.transformer import TransformerAgent


def test_student_mapping_fields():
    # source_id: the generic wire-format key every adapter's fetch
    # functions alias their own primary key to (see pieas_source.py).
    record = {
        "source_id": "PIEAS-STU-1", "roll_number": "2024-CS-001", "first_name": "Ada",
        "last_name": "Lovelace", "email": "ada@example.com", "gender": "female",
        "date_of_birth": "2003-01-01", "department": "Computer Science", "batch_year": 2024,
        "last_updated": "2026-01-01T00:00:00",
    }
    out = TransformerAgent.transform("student", record)
    assert out["name"] == "Ada Lovelace"
    assert out["pieas_id"] == "PIEAS-STU-1"  # mock target's own schema column name
    assert out["birth_date"] == "2003-01-01"
    assert "last_updated" not in out  # source-only bookkeeping field must not leak into OpenEduCat


def test_faculty_mapping_fields():
    record = {
        "source_id": "PIEAS-FAC-1", "employee_code": "EMP-1", "first_name": "Grace",
        "last_name": "Hopper", "email": "grace@example.com", "department": "Computer Science",
        "designation": "Professor", "last_updated": "2026-01-01T00:00:00",
    }
    out = TransformerAgent.transform("faculty", record)
    assert out["name"] == "Grace Hopper"
    assert out["designation"] == "Professor"


def test_course_mapping_fields():
    record = {
        "source_id": "PIEAS-CRS-1", "code": "CS-301", "name": "Algorithms",
        "department": "Computer Science", "credit_hours": 3, "semester": "Fall",
        "last_updated": "2026-01-01T00:00:00",
    }
    out = TransformerAgent.transform("course", record)
    assert out["code"] == "CS-301"
    assert out["credit_hours"] == 3


def test_unknown_entity_type_raises():
    with pytest.raises(ValueError):
        TransformerAgent.transform("bogus", {"source_id": "x"})


def test_transform_is_pure_and_order_independent():
    """Same record in -> same record out, regardless of batch context; the
    Transformer must not accumulate any state across calls.
    """
    record = {
        "source_id": "PIEAS-CRS-1", "code": "CS-301", "name": "Algorithms",
        "department": "Computer Science", "credit_hours": 3, "semester": "Fall",
        "last_updated": "2026-01-01T00:00:00",
    }
    first = TransformerAgent.transform("course", record)
    second = TransformerAgent.transform("course", record)
    assert first == second


def test_transform_mark_maps_relational_refs_as_plain_text_for_the_mock():
    from openedu_orchestrator.agents.transformer import TransformerAgent

    out = TransformerAgent.transform("mark", {
        "source_id": "PIEAS-MRK-1", "student_pieas_id": "PIEAS-STU-1",
        "course_pieas_id": "PIEAS-CRS-1", "exam_name": "Midterm",
        "marks_obtained": 37, "total_marks": 50, "last_updated": "2026-01-01T00:00:00",
    })
    assert out["student_ref"] == "PIEAS-STU-1"
    assert out["marks"] == 37 and out["total_marks"] == 50
    assert out["status"] == "present"
