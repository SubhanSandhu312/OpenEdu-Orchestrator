"""The Loader: create, update, archive, and what happens when Odoo says no."""
import state
from agents import Loader
from conftest import FakeOdoo, STUDENT_FIELDS


def make_loader(**kwargs):
    odoo = FakeOdoo({"op.student": STUDENT_FIELDS, "op.exam.attendees": {}}, **kwargs)
    return Loader(odoo), odoo


def create(pieas_id, **vals):
    return {"pieas_id": pieas_id, "op": "create", "vals": vals, "hash": f"h{pieas_id}"}


def test_create_then_update_keeps_the_mapping_in_step():
    loader, odoo = make_loader()

    stats = loader.load("students", "op.student", [create(1, name="Ayesha")])
    odoo_id = state.lookup_odoo_id("students", 1)
    assert stats["created"] == 1
    assert odoo.records[("op.student", odoo_id)]["name"] == "Ayesha"

    stats = loader.load("students", "op.student", [
        {"pieas_id": 1, "op": "write", "odoo_id": odoo_id,
         "vals": {"name": "Ayesha Malik"}, "hash": "h1b"}])
    assert stats["updated"] == 1
    assert odoo.records[("op.student", odoo_id)]["name"] == "Ayesha Malik"
    assert state.lookup("students", 1)["row_hash"] == "h1b"


def test_archive_sets_active_false_and_marks_the_mapping():
    loader, odoo = make_loader()
    loader.load("students", "op.student", [create(1, name="Ayesha")])
    odoo_id = state.lookup_odoo_id("students", 1)

    stats = loader.load("students", "op.student", [
        {"pieas_id": 1, "op": "archive", "odoo_id": odoo_id, "hash": "h1"}])

    assert (stats["archived"], stats["deleted"]) == (1, 0)
    assert odoo.records[("op.student", odoo_id)]["active"] is False
    assert state.lookup("students", 1)["status"] == "archived"


def test_models_without_an_active_field_are_deleted_and_counted_separately():
    loader, odoo = make_loader(no_active_field=("op.exam.attendees",))
    loader.load("exam_results", "op.exam.attendees", [create(1, marks=71)])
    odoo_id = state.lookup_odoo_id("exam_results", 1)

    stats = loader.load("exam_results", "op.exam.attendees", [
        {"pieas_id": 1, "op": "archive", "odoo_id": odoo_id, "hash": "h1"}])

    assert (stats["archived"], stats["deleted"]) == (0, 1)
    assert ("op.exam.attendees", odoo_id) not in odoo.records


def test_a_failed_archive_is_never_retried_as_a_create(llm):
    loader, odoo = make_loader()
    loader.load("students", "op.student", [create(1, name="Ayesha")])
    odoo_id = state.lookup_odoo_id("students", 1)
    odoo.fail_archive = True
    llm.responder = lambda prompt: {"action": "retry"}

    stats = loader.load("students", "op.student", [
        {"pieas_id": 1, "op": "archive", "odoo_id": odoo_id, "hash": "h1"}])

    assert stats["failed"] == 1
    assert llm.prompts == [], "an archive failure must not be sent for triage"
    assert len([c for c in odoo.calls if c[0] == "create"]) == 1, "no duplicate record"
    assert state.lookup("students", 1)["status"] == "active"


def test_triage_can_drop_the_offending_field_and_retry(llm):
    loader, odoo = make_loader()
    odoo.reject_field = {"gr_no": "ValidationError: gr_no must be unique"}
    llm.responder = lambda prompt: {"action": "drop_field", "field": "gr_no"}

    stats = loader.load("students", "op.student", [create(1, name="Ayesha", gr_no="DUP")])

    assert (stats["created"], stats["failed"]) == (1, 0)
    saved = odoo.records[("op.student", state.lookup_odoo_id("students", 1))]
    assert "gr_no" not in saved and saved["name"] == "Ayesha"


def test_the_same_error_is_triaged_once_not_once_per_record(llm):
    loader, odoo = make_loader()
    odoo.reject_field = {"gr_no": "ValidationError: gr_no must be unique"}
    llm.responder = lambda prompt: {"action": "drop_field", "field": "gr_no"}

    loader.load("students", "op.student",
                [create(i, name=f"S{i}", gr_no="DUP") for i in range(1, 6)])

    assert len(llm.prompts) == 1


def test_a_record_is_skipped_and_counted_when_the_llm_is_unreachable(llm):
    loader, odoo = make_loader()
    odoo.reject_field = {"gr_no": "ValidationError: gr_no must be unique"}

    stats = loader.load("students", "op.student", [create(1, name="Ayesha", gr_no="DUP")])

    assert (stats["created"], stats["failed"]) == (0, 1)
    assert state.lookup("students", 1) is None
    assert stats["errors"] == ["students#1: ValidationError: gr_no must be unique"]


def test_an_unknown_triage_action_is_treated_as_skip(llm):
    loader, odoo = make_loader()
    odoo.reject_field = {"gr_no": "boom"}
    llm.responder = lambda prompt: {"action": "delete_everything"}

    stats = loader.load("students", "op.student", [create(1, gr_no="DUP")])

    assert stats["failed"] == 1


def test_only_the_first_five_errors_are_kept(llm):
    loader, odoo = make_loader()
    odoo.reject_field = {"gr_no": "boom"}

    stats = loader.load("students", "op.student",
                        [create(i, gr_no="DUP") for i in range(1, 9)])

    assert stats["failed"] == 8
    assert len(stats["errors"]) == 5
