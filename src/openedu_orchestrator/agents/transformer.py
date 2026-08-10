"""Transformer Agent -- stateless mapping from PIEAS schema to OpenEduCat schema.

Per the report (Section 3.3): "Implemented as a pure function (record in,
OpenEduCat-shaped record out); identical logic regardless of which cycle
triggered it or how many records it is given." No I/O, no database
connections, no knowledge of create vs. update -- that decision was already
made by the Orchestrator before a record reaches here.
"""

from __future__ import annotations

from typing import Callable


def _map_student(record: dict) -> dict:
    return {
        "pieas_id": record["source_id"],  # mock's own schema column; record's key is now generic
        "roll_number": record["roll_number"],
        "first_name": record["first_name"],
        "last_name": record["last_name"],
        "name": f"{record['first_name']} {record['last_name']}",
        "email": record["email"],
        "gender": record["gender"],
        "birth_date": record["date_of_birth"],
        "department": record["department"],
        "batch_year": record["batch_year"],
    }


def _map_faculty(record: dict) -> dict:
    return {
        "pieas_id": record["source_id"],  # mock's own schema column; record's key is now generic
        "employee_code": record["employee_code"],
        "first_name": record["first_name"],
        "last_name": record["last_name"],
        "name": f"{record['first_name']} {record['last_name']}",
        "email": record["email"],
        "department": record["department"],
        "designation": record["designation"],
    }


def _map_course(record: dict) -> dict:
    return {
        "pieas_id": record["source_id"],  # mock's own schema column; record's key is now generic
        "code": record["code"],
        "name": record["name"],
        "department": record["department"],
        "credit_hours": record["credit_hours"],
        "semester": record["semester"],
    }


def _map_mark(record: dict) -> dict:
    """The mock target keeps the student/subject references as plain text
    rather than resolving them to ids -- see op_exam_attendees' schema
    comment. Against the real target these become "reference" sentinels
    resolved to real many2one ids instead (mappings/mark_pieas.json).
    """
    return {
        "pieas_id": record["source_id"],  # mock's own schema column; record's key is generic
        "student_ref": record["student_pieas_id"],
        "subject_ref": record["course_pieas_id"],
        "exam_name": record["exam_name"],
        "marks": record["marks_obtained"],
        "total_marks": record["total_marks"],
        "status": "present",
    }


_MAPPERS: dict[str, Callable[[dict], dict]] = {
    "student": _map_student,
    "faculty": _map_faculty,
    "course": _map_course,
    "mark": _map_mark,
}


class TransformerAgent:
    @staticmethod
    def transform(entity_type: str, record: dict) -> dict:
        try:
            mapper = _MAPPERS[entity_type]
        except KeyError as exc:
            raise ValueError(f"No transform mapping for entity_type={entity_type!r}") from exc
        return mapper(record)

    @classmethod
    def transform_batch(cls, entity_type: str, records: list[dict]) -> list[dict]:
        return [cls.transform(entity_type, record) for record in records]
