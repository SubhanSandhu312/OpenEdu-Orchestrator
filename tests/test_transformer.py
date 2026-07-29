from __future__ import annotations

import pytest

from openedu_orchestrator.agents.transformer import TransformerAgent


def test_student_mapping_fields():
    record = {
        "pieas_id": "PIEAS-STU-1", "roll_number": "2024-CS-001", "first_name": "Ada",
        "last_name": "Lovelace", "email": "ada@example.com", "gender": "female",
        "date_of_birth": "2003-01-01", "department": "Computer Science", "batch_year": 2024,
        "last_updated": "2026-01-01T00:00:00",
    }
    out = TransformerAgent.transform("student", record)
    assert out["name"] == "Ada Lovelace"
    assert out["pieas_id"] == "PIEAS-STU-1"
    assert out["birth_date"] == "2003-01-01"
    assert "last_updated" not in out  # PIEAS-only bookkeeping field must not leak into OpenEduCat


def test_faculty_mapping_fields():
    record = {
        "pieas_id": "PIEAS-FAC-1", "employee_code": "EMP-1", "first_name": "Grace",
        "last_name": "Hopper", "email": "grace@example.com", "department": "Computer Science",
        "designation": "Professor", "last_updated": "2026-01-01T00:00:00",
    }
    out = TransformerAgent.transform("faculty", record)
    assert out["name"] == "Grace Hopper"
    assert out["designation"] == "Professor"


def test_course_mapping_fields():
    record = {
        "pieas_id": "PIEAS-CRS-1", "code": "CS-301", "name": "Algorithms",
        "department": "Computer Science", "credit_hours": 3, "semester": "Fall",
        "last_updated": "2026-01-01T00:00:00",
    }
    out = TransformerAgent.transform("course", record)
    assert out["code"] == "CS-301"
    assert out["credit_hours"] == 3


def test_unknown_entity_type_raises():
    with pytest.raises(ValueError):
        TransformerAgent.transform("bogus", {"pieas_id": "x"})


def test_transform_is_pure_and_order_independent():
    """Same record in -> same record out, regardless of batch context; the
    Transformer must not accumulate any state across calls.
    """
    record = {
        "pieas_id": "PIEAS-CRS-1", "code": "CS-301", "name": "Algorithms",
        "department": "Computer Science", "credit_hours": 3, "semester": "Fall",
        "last_updated": "2026-01-01T00:00:00",
    }
    first = TransformerAgent.transform("course", record)
    second = TransformerAgent.transform("course", record)
    assert first == second
