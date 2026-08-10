"""Reference-data resolution for the real OpenEduCat target -- department,
program, batch, and enrollment records that PIEAS's flat schema has no
direct equivalent for.

Not part of the deterministic sync pipeline's per-record field mapping
(that's what mapping_authoring.py's compiled transform_fn handles); this is
a separate, idempotent get-or-create layer, because these are *shared*
reference records (many students belong to the same department/batch), not
one-record-per-PIEAS-row data. Called before/around student creation, not
threaded through the generic Loader/Transformer path.

Real op.student has no plain department/batch_year field at all -- it's
reached via course_detail_ids (op.student.course, an enrollment record) ->
op.batch -> op.course (the *degree program*, not a subject). PIEAS's own
"courses" table turned out to represent subjects, not programs (see
mappings/course_pieas.json's rework to target op.subject instead) -- so a
synthetic per-department "program" op.course is created here purely to
give op.batch something to attach to. This is a genuine modeling
compromise: PIEAS's dummy schema has no program/degree concept of its own,
the same class of gap as the earlier faculty-gender omission.
"""

from __future__ import annotations

from datetime import date
from typing import Any

DEPARTMENT_CODES = {
    "Computer Science": "CS",
    "Electrical Engineering": "EE",
    "Mechanical Engineering": "ME",
    "Chemical Engineering": "CHE",
    "Metallurgy & Materials Engineering": "MME",
    "Nuclear Engineering": "NE",
    "Physics": "PHY",
    "Mathematics": "MATH",
    "Management Sciences": "MS",
}


def get_or_create_department(client: Any, name: str) -> int:
    existing = client.search_read("op.department", [("name", "=", name)], fields=["id"])
    if existing:
        return existing[0]["id"]
    code = DEPARTMENT_CODES.get(name, name[:4].upper())
    return client.create("op.department", {"name": name, "code": code})


def get_or_create_program_course(client: Any, department_id: int, department_name: str) -> int:
    """A synthetic per-department "program" op.course, purely so op.batch
    has something to link to -- see module docstring for why this isn't a
    real PIEAS-sourced mapping.
    """
    code = f"PROG-{DEPARTMENT_CODES.get(department_name, department_name[:4].upper())}"
    existing = client.search_read("op.course", [("code", "=", code)], fields=["id"])
    if existing:
        return existing[0]["id"]
    return client.create("op.course", {
        "name": f"{department_name} Program",
        "code": code,
        "department_id": department_id,
    })


def get_or_create_batch(client: Any, course_id: int, batch_year: int) -> int:
    code = f"BATCH-{batch_year}-{course_id}"
    existing = client.search_read("op.batch", [("code", "=", code)], fields=["id"])
    if existing:
        return existing[0]["id"]
    return client.create("op.batch", {
        "name": f"Batch {batch_year}",
        "code": code,
        "course_id": course_id,
        "start_date": date(batch_year, 9, 1).isoformat(),
        "end_date": date(batch_year + 4, 6, 30).isoformat(),
    })


def resolve_department_and_batch(client: Any, department_name: str, batch_year: int) -> dict:
    """One call that does the full department -> program -> batch chain,
    returning everything needed to both write op.student's implicit
    department context and create its enrollment record.
    """
    department_id = get_or_create_department(client, department_name)
    course_id = get_or_create_program_course(client, department_id, department_name)
    batch_id = get_or_create_batch(client, course_id, batch_year)
    return {"department_id": department_id, "course_id": course_id, "batch_id": batch_id}


def enroll_student(client: Any, student_id: int, course_id: int, batch_id: int) -> int:
    """Create the op.student.course enrollment record linking a student to
    their resolved batch/program. Idempotent: won't duplicate if the
    student is already enrolled in this exact batch.
    """
    existing = client.search_read(
        "op.student.course",
        [("student_id", "=", student_id), ("batch_id", "=", batch_id)],
        fields=["id"],
    )
    if existing:
        return existing[0]["id"]
    return client._execute("op.student.course", "create", [{
        "student_id": student_id, "course_id": course_id, "batch_id": batch_id,
    }])
