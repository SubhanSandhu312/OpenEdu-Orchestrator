"""The Transformer: the model proposes a mapping, the live schema decides."""
import pytest

import state
from agents import Transformer

sanitize = Transformer._sanitize


def proposal(**overrides):
    plan = {
        "fields": {
            "reg_no":     {"target": "gr_no", "cast": "str"},
            "first_name": {"target": "first_name"},
            "last_name":  {"target": "last_name", "cast": "str"},
            "gender":     {"target": "gender", "values": {"M": "m", "F": "f"}},
        },
        "relations": {"department_id": {"target": "department_id", "entity": "departments"}},
        "computed": {"name": "{first_name} {last_name}"},
        "defaults": {},
    }
    plan.update(overrides)
    return plan


def test_a_clean_proposal_survives_intact(student_schema, student_fields):
    plan = sanitize(proposal(), student_fields, student_schema, "op.student", "students")

    assert set(plan["fields"]) == {"reg_no", "first_name", "last_name", "gender"}
    assert plan["fields"]["first_name"]["cast"] == "str"          # default cast
    assert plan["relations"] == {"department_id": {"target": "department_id",
                                                   "entity": "departments"}}
    assert plan["computed"] == {"name": "{first_name} {last_name}"}
    assert plan["dropped"] == []
    assert plan["unmet_required"] == []
    assert Transformer._usable(plan)


def test_invented_target_fields_are_dropped(student_schema, student_fields):
    fields = proposal()["fields"]
    fields["reg_no"] = {"target": "student_number"}               # not a real Odoo field
    plan = sanitize(proposal(fields=fields), student_fields, student_schema,
                    "op.student", "students")

    assert "reg_no" not in plan["fields"]
    assert "field reg_no->student_number" in plan["dropped"]


def test_invented_source_columns_and_bookkeeping_columns_are_ignored(student_schema, student_fields):
    fields = proposal()["fields"]
    fields["nickname"] = {"target": "gr_no"}                      # no such source column
    fields["id"] = {"target": "gr_no"}
    fields["last_updated"] = {"target": "gr_no"}
    plan = sanitize(proposal(fields=fields), student_fields, student_schema,
                    "op.student", "students")

    assert {"nickname", "id", "last_updated"}.isdisjoint(plan["fields"])


def test_a_literal_id_for_a_relational_field_is_never_accepted(student_schema, student_fields):
    plan = sanitize(proposal(defaults={"partner_id": 1, "gr_no": "UNKNOWN"}),
                    student_fields, student_schema, "op.student", "students")

    assert plan["defaults"] == {"gr_no": "UNKNOWN"}
    assert "default partner_id (relational literal)" in plan["dropped"]


def test_a_foreign_key_the_model_missed_is_recovered_from_the_schema(student_schema, student_fields):
    plan = sanitize(proposal(relations={}), student_fields, student_schema,
                    "op.student", "students")

    assert plan["relations"] == {"department_id": {"target": "department_id",
                                                   "entity": "departments"}}


def test_relations_to_tables_outside_the_allow_list_are_dropped(student_schema, student_fields):
    relations = {"department_id": {"target": "department_id", "entity": "payroll"}}
    student_schema["foreign_keys"] = {}
    plan = sanitize(proposal(relations=relations), student_fields, student_schema,
                    "op.student", "students")

    assert plan["relations"] == {}
    assert "relation department_id->department_id" in plan["dropped"]


def test_missing_required_fields_are_reported_and_make_the_plan_unusable(student_schema, student_fields):
    fields = proposal()["fields"]
    del fields["gender"]
    plan = sanitize(proposal(fields=fields), student_fields, student_schema,
                    "op.student", "students")

    assert plan["unmet_required"] == ["gender"]
    assert not Transformer._usable(plan)


def test_required_relational_fields_do_not_count_as_unmet(student_schema, student_fields):
    # partner_id is required and many2one: Odoo creates it through _inherits.
    plan = sanitize(proposal(), student_fields, student_schema, "op.student", "students")
    assert "partner_id" not in plan["unmet_required"]


@pytest.mark.xfail(strict=True, reason="Known issue, HANDOFF.md section 10.2: _sanitize() does not "
                                       "check that a relation's target is a many2one field.")
def test_a_relation_pointed_at_a_non_relational_field_is_dropped(student_schema, student_fields):
    relations = {"department_id": {"target": "gr_no", "entity": "departments"}}
    plan = sanitize(proposal(relations=relations), student_fields, student_schema,
                    "op.student", "students")

    assert "department_id" not in plan["relations"] or \
        plan["relations"]["department_id"]["target"] != "gr_no"


@pytest.mark.parametrize("value, kind, expected", [
    ("42", "int", 42),
    ("42.9", "int", 42),
    ("3.5", "float", 3.5),
    ("yes", "bool", True),
    ("Present", "bool", True),
    ("0", "bool", False),
    ("2026-08-10 14:30:00", "date", "2026-08-10"),
    ("2026-08-10", "datetime", "2026-08-10 00:00:00"),
    ("2026-08-10 14:30:00.123", "datetime", "2026-08-10 14:30:00"),
    (17, "str", "17"),
    (None, "int", None),
    ("abc", "int", None),
])
def test_cast(value, kind, expected):
    assert Transformer._cast(value, kind) == expected


def apply(plan, row):
    return Transformer(pieas=None, odoo=None).apply(plan, row)


def test_apply_maps_values_resolves_relations_and_builds_computed_fields(student_schema, student_fields):
    plan = sanitize(proposal(), student_fields, student_schema, "op.student", "students")
    state.upsert("departments", 3, "op.department", 501, "h")
    row = {"id": 9, "reg_no": "PIEAS-25-EE-009", "first_name": "Ayesha",
           "last_name": "Malik", "gender": "F", "department_id": 3,
           "last_updated": "2026-08-10 09:00:00"}

    vals, warnings = apply(plan, row)

    assert vals == {"gr_no": "PIEAS-25-EE-009", "first_name": "Ayesha",
                    "last_name": "Malik", "gender": "f", "department_id": 501,
                    "name": "Ayesha Malik"}
    assert warnings == []


def test_apply_warns_instead_of_guessing_when_a_parent_is_not_synced_yet(student_schema, student_fields):
    plan = sanitize(proposal(), student_fields, student_schema, "op.student", "students")
    row = {"id": 9, "reg_no": "X", "first_name": "A", "last_name": "B",
           "gender": "M", "department_id": 3}

    vals, warnings = apply(plan, row)

    assert "department_id" not in vals
    assert warnings == ["unresolved department_id=3 -> departments"]


def test_apply_skips_nulls_and_uses_defaults_only_for_missing_fields(student_schema, student_fields):
    plan = sanitize(proposal(defaults={"gr_no": "UNKNOWN", "gender": "m"}),
                    student_fields, student_schema, "op.student", "students")
    row = {"id": 9, "reg_no": None, "first_name": "Bilal", "last_name": None,
           "gender": "F", "department_id": None}

    vals, _ = apply(plan, row)

    assert vals["gr_no"] == "UNKNOWN"        # null in the source: default fills it
    assert vals["gender"] == "f"             # present in the source: default ignored
    assert "last_name" not in vals
    assert vals["name"] == "Bilal"           # computed field collapses the gap
