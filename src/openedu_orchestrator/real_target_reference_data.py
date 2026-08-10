"""Reference-data resolution for the real OpenEduCat target -- department,
program, batch, and enrollment records that PIEAS's flat schema has no
direct equivalent for.

Not part of the deterministic sync pipeline's per-record field mapping
(that's what mapping_authoring.py's compiled transform_fn handles); this is
a separate, idempotent get-or-create layer, because these are *shared*
reference records (many students belong to the same department/batch), not
one-record-per-PIEAS-row data.

It is reached from OdooXmlRpcClient, which pops the REFERENCE_PREFIX
sentinels the compiled transform passed through and calls resolve() below.
That placement is deliberate: resolving a reference means talking to the
target, so it cannot happen in the Transformer without breaking the
report's Section 3.3 requirement that the Transformer be a pure function,
and it should not happen in the Loader, whose job is to execute exactly the
action it was given. The client is the target adapter, so "how this
particular target represents a student's department" belongs here.

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
from typing import Any, Callable, Optional

# Sentinel prefix for reference values travelling from the (pure) compiled
# transform to the client, which is the only layer allowed to talk to the
# target and therefore the only layer that can resolve them. Same pattern as
# the "source_id" sentinel used for external IDs.
REFERENCE_PREFIX = "__ref__"

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


def get_or_create_exam(client: Any, subject_id: int, exam_name: str, total_marks: int) -> int:
    """An op.exam for a (subject, exam name) pair. Shared reference data:
    every student's mark for the same midterm points at one exam record.

    op.exam requires start/end times it has no source equivalent for -- PIEAS
    records the mark, not when the paper was sat. Placeholder times are used
    and disclosed here rather than fabricated silently, the same stance taken
    for op.subject's required subject_type in mappings/course_pieas.json.
    """
    code = f"EXAM-{subject_id}-{exam_name.upper().replace(' ', '-')}"
    existing = client.search_read("op.exam", [("exam_code", "=", code)], fields=["id"])
    if existing:
        return existing[0]["id"]
    return client.create("op.exam", {
        "name": exam_name,
        "exam_code": code,
        "subject_id": subject_id,
        "total_marks": int(total_marks),
        "min_marks": int(int(total_marks) * 0.4),  # 40% pass mark, a disclosed assumption
        "start_time": "2026-01-01 09:00:00",
        "end_time": "2026-01-01 12:00:00",
    })


# --- reference resolution dispatch -------------------------------------
#
# Each handler takes the sentinel values the compiled transform passed
# through and returns:
#   (field_updates, post_write_hook)
# where field_updates are merged into the values actually written, and
# post_write_hook (if any) is called with the record id after the write --
# for links that can only be made once the record exists.

def _student_references(client: Any, refs: dict) -> tuple[dict, Optional[Callable[[int], None]]]:
    """op.student has no plain department or batch_year field: both are
    reached through an op.student.course enrollment record, which can only
    be created after the student exists. Hence a post-write hook.
    """
    department, batch_year = refs.get("department"), refs.get("batch_year")
    if not department or not batch_year:
        return {}, None
    ids = resolve_department_and_batch(client, department, int(batch_year))

    def _enroll(student_id: int) -> None:
        enroll_student(client, student_id, ids["course_id"], ids["batch_id"])

    return {}, _enroll


def _exam_attendee_references(client: Any, refs: dict) -> tuple[dict, Optional[Callable[[int], None]]]:
    """A mark points at two already-synced records (a student and a subject)
    plus one derived shared record (the exam). The first two are resolved
    through the same ir.model.data external IDs the pipeline registered when
    it synced them -- which is what makes cross-entity references work
    without a second mapping store.
    """
    student_ref, subject_ref = refs.get("student"), refs.get("subject")
    student_id = client.find_by_external_id("op.student", student_ref) if student_ref else None
    subject_id = client.find_by_external_id("op.subject", subject_ref) if subject_ref else None
    if student_id is None or subject_id is None:
        missing = "student" if student_id is None else "subject"
        raise ValueError(
            f"cannot sync mark: its {missing} has not been synced to the target yet "
            f"(student={student_ref!r}, subject={subject_ref!r}). "
            f"Sync student and course before mark."
        )
    exam_id = get_or_create_exam(
        client, subject_id, refs.get("exam_name") or "Exam", refs.get("total_marks") or 100
    )
    return {"student_id": student_id, "exam_id": exam_id}, None


_RESOLVERS: dict[str, Callable[[Any, dict], tuple[dict, Optional[Callable[[int], None]]]]] = {
    "op.student": _student_references,
    "op.exam.attendees": _exam_attendee_references,
}


def split_references(values: dict) -> tuple[dict, dict]:
    """Separate the __ref__* sentinels from the real field values."""
    plain, refs = {}, {}
    for key, value in values.items():
        if key.startswith(REFERENCE_PREFIX):
            refs[key[len(REFERENCE_PREFIX):]] = value
        else:
            plain[key] = value
    return plain, refs


def resolve(client: Any, model: str, refs: dict) -> tuple[dict, Optional[Callable[[int], None]]]:
    """Resolve a model's reference sentinels. Unknown models resolve to
    nothing rather than raising: a target model with no reference handler
    simply has no shared records to link, which is the common case.
    """
    if not refs:
        return {}, None
    handler = _RESOLVERS.get(model)
    if handler is None:
        return {}, None
    return handler(client, refs)
