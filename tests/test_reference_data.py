from __future__ import annotations

import pytest

from openedu_orchestrator import real_target_reference_data as refdata
from openedu_orchestrator.agents.validator import _is_client_sentinel


class FakeClient:
    """Records every create/search so reference resolution can be tested
    without a live Odoo. Deliberately mimics the get-or-create contract:
    search_read returns what has already been created.
    """

    def __init__(self, external_ids: dict | None = None):
        self.created: list[tuple[str, dict]] = []
        self._rows: dict[str, list[dict]] = {}
        self._next_id = 1
        self._external_ids = external_ids or {}

    def create(self, model, values):
        rec = dict(values)
        rec["id"] = self._next_id
        self._next_id += 1
        self._rows.setdefault(model, []).append(rec)
        self.created.append((model, values))
        return rec["id"]

    def search_read(self, model, domain=None, fields=None):
        rows = self._rows.get(model, [])
        for field, _op, value in domain or []:
            rows = [r for r in rows if r.get(field) == value]
        return rows

    def _execute(self, model, method, args, kwargs=None):
        if method == "create":
            return self.create(model, args[0])
        if method == "search_read":
            return self.search_read(model, args[0] if args else [])
        raise AssertionError(f"unexpected {model}.{method}")

    def find_by_external_id(self, model, external_id):
        return self._external_ids.get((model, external_id))


def test_split_references_separates_sentinels_from_real_fields():
    plain, refs = refdata.split_references(
        {"gr_no": "R1", "__ref__department": "Physics", "__ref__batch_year": 2027}
    )
    assert plain == {"gr_no": "R1"}
    assert refs == {"department": "Physics", "batch_year": 2027}


def test_resolve_returns_nothing_for_a_model_with_no_handler():
    """A target model with no shared records to link is the common case and
    must not be an error.
    """
    assert refdata.resolve(FakeClient(), "op.faculty", {"anything": 1}) == ({}, None)


def test_resolve_returns_nothing_when_there_are_no_references():
    assert refdata.resolve(FakeClient(), "op.student", {}) == ({}, None)


def test_student_references_build_department_program_and_batch():
    client = FakeClient()
    fields, post = refdata.resolve(
        client, "op.student", {"department": "Physics", "batch_year": 2027}
    )
    assert fields == {}, "op.student has no plain department field to write"
    assert post is not None, "enrollment can only happen after the student exists"
    models = [m for m, _ in client.created]
    assert models == ["op.department", "op.course", "op.batch"]


def test_student_enrollment_is_idempotent():
    """Re-syncing an unchanged student must not add a second enrollment."""
    client = FakeClient()
    _, post = refdata.resolve(client, "op.student", {"department": "Physics", "batch_year": 2027})
    post(42)
    post(42)
    enrollments = [m for m, _ in client.created if m == "op.student.course"]
    assert len(enrollments) == 1


def test_reference_records_are_shared_between_students():
    """Two students in the same department/year must reuse one batch, not
    create one each -- the whole reason these are "reference" data.
    """
    client = FakeClient()
    refdata.resolve(client, "op.student", {"department": "Physics", "batch_year": 2027})
    refdata.resolve(client, "op.student", {"department": "Physics", "batch_year": 2027})
    assert [m for m, _ in client.created] == ["op.department", "op.course", "op.batch"]


def test_mark_references_resolve_student_and_subject_by_external_id():
    client = FakeClient(external_ids={
        ("op.student", "PIEAS-STU-1"): 11,
        ("op.subject", "PIEAS-CRS-1"): 22,
    })
    fields, post = refdata.resolve(client, "op.exam.attendees", {
        "student": "PIEAS-STU-1", "subject": "PIEAS-CRS-1",
        "exam_name": "Midterm", "total_marks": 50,
    })
    assert fields["student_id"] == 11
    assert post is None
    exam = dict(client.created[0][1])
    assert client.created[0][0] == "op.exam"
    assert exam["subject_id"] == 22 and exam["total_marks"] == 50


def test_mark_reuses_one_exam_across_students():
    client = FakeClient(external_ids={
        ("op.student", "S1"): 11, ("op.student", "S2"): 12, ("op.subject", "C1"): 22,
    })
    for s in ("S1", "S2"):
        refdata.resolve(client, "op.exam.attendees", {
            "student": s, "subject": "C1", "exam_name": "Midterm", "total_marks": 50,
        })
    assert len([m for m, _ in client.created if m == "op.exam"]) == 1


def test_mark_fails_clearly_when_its_student_is_not_synced_yet():
    """Cross-entity dependency: a mark cannot be written before the student
    it refers to. The error must name the problem, not surface as a
    confusing false/None foreign key.
    """
    client = FakeClient(external_ids={("op.subject", "C1"): 22})
    with pytest.raises(ValueError, match="has not been synced"):
        refdata.resolve(client, "op.exam.attendees", {
            "student": "MISSING", "subject": "C1", "exam_name": "Midterm", "total_marks": 50,
        })


def test_validator_treats_reference_sentinels_as_client_internal():
    assert _is_client_sentinel("__ref__department") is True
    assert _is_client_sentinel("source_id") is True
    assert _is_client_sentinel("first_name") is False


def test_compiled_mark_mapping_names_references_by_target_field():
    """The resolver dispatches on the reference *name*, so a mapping must be
    able to call student_pieas_id "student" -- otherwise the resolver would
    have to know each source's column naming, defeating the generalisation.
    Found by a live run where the sentinels came through as
    __ref__student_pieas_id and the resolver saw None.
    """
    from pathlib import Path
    from openedu_orchestrator import mapping_authoring as ma

    fn = ma.compile_mapping(ma.load_mapping(Path("mappings/mark_pieas.json")))
    out = fn("mark", {
        "source_id": "PIEAS-MRK-1", "student_pieas_id": "PIEAS-STU-1",
        "course_pieas_id": "PIEAS-CRS-1", "exam_name": "Midterm",
        "marks_obtained": 37, "total_marks": 50,
    })
    assert out["__ref__student"] == "PIEAS-STU-1"
    assert out["__ref__subject"] == "PIEAS-CRS-1"
    assert out["marks"] == 37


def test_compiled_mapping_applies_human_approved_constants():
    """op.exam.attendees.status is required with no source equivalent. The
    constant is a human review decision recorded in the approved mapping,
    never something the LLM proposal schema can invent.
    """
    from pathlib import Path
    from openedu_orchestrator import mapping_authoring as ma

    fn = ma.compile_mapping(ma.load_mapping(Path("mappings/mark_pieas.json")))
    out = fn("mark", {
        "source_id": "M1", "student_pieas_id": "S1", "course_pieas_id": "C1",
        "exam_name": "Final", "marks_obtained": 1, "total_marks": 10,
    })
    assert out["status"] == "present"
