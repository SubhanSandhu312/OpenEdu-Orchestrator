"""Tests for the design-time mapping-authoring tool (mapping_authoring.py).

Covers everything that doesn't require a live model call: source/target
schema extraction, and compiling an approved config into a transform_fn.
The compile_mapping test uses today's actual hand-verified real-target
mapping (the one that successfully bulk-migrated 60 students to a live
Odoo 19.0 + OpenEduCat 19.0 instance) as ground truth, not just
self-consistency -- see docs/mapping_authoring_tool.md.
"""

from __future__ import annotations

import json

import pytest

from openedu_orchestrator import mapping_authoring as ma


def test_extract_source_schema_student():
    schema = ma.extract_source_schema("student")
    assert "roll_number" in schema
    assert "gender" in schema
    assert schema["roll_number"]["required"] is True


def test_extract_source_schema_unknown_entity_raises():
    with pytest.raises(ValueError):
        ma.extract_source_schema("bogus")


class _StubClient:
    """Minimal stand-in for OdooXmlRpcClient's _execute, so this stays a
    fast unit test independent of any live Odoo instance -- mirrors the
    real fields_get shape observed against the live instance.
    """

    def _execute(self, model, method, args=None, kwargs=None):
        assert method == "fields_get"
        return {
            "gr_no": {"string": "Registration Number", "type": "char", "required": False},
            "gender": {
                "string": "Gender", "type": "selection", "required": True,
                "selection": [["m", "Male"], ["f", "Female"], ["o", "Other"]],
            },
        }


def test_extract_target_schema():
    schema = ma.extract_target_schema(_StubClient(), "op.student")
    assert schema["gr_no"]["label"] == "Registration Number"
    assert schema["gender"]["required"] is True
    assert schema["gender"]["selection"] == [["m", "Male"], ["f", "Female"], ["o", "Other"]]


def test_build_mapping_prompt_includes_both_schemas_and_samples():
    prompt = ma.build_mapping_prompt(
        "student",
        {"roll_number": {"type": "str", "required": True}},
        {"gr_no": {"label": "Registration Number", "type": "char", "required": False, "selection": None}},
        sample_records=[{"roll_number": "2024-CS-001"}],
    )
    assert "roll_number" in prompt
    assert "gr_no" in prompt
    assert "2024-CS-001" in prompt


def test_call_llm_raises_clearly_when_unwired():
    with pytest.raises(NotImplementedError, match="ANTHROPIC_API_KEY"):
        ma._call_llm("irrelevant prompt")


# --- compile_mapping: golden-output test against today's real, live-verified mapping ---

APPROVED_STUDENT_MAPPING = {
    "entity_type": "student",
    "target_model": "op.student",
    "field_mappings": [
        {"source_field": "pieas_id", "handling": "external_id"},
        {"source_field": "roll_number", "target_field": "gr_no", "handling": "direct"},
        {"source_field": "first_name", "handling": "direct"},
        {"source_field": "last_name", "handling": "direct"},
        {
            "source_field": "gender", "target_field": "gender", "handling": "value_map",
            "value_map": {"male": "m", "female": "f"},
        },
        {"source_field": "date_of_birth", "target_field": "birth_date", "handling": "direct"},
        {
            "source_field": "department", "handling": "unmapped",
            "note": "op.student has no direct department field; department is relational "
                    "via course enrollment (course_detail_ids), not a plain field.",
        },
        {
            "source_field": "batch_year", "handling": "unmapped",
            "note": "same as department -- relational, not a plain field.",
        },
    ],
    "unmapped_required_target_fields": [],
}

SAMPLE_PIEAS_STUDENT = {
    "pieas_id": "PIEAS-STU-1",
    "roll_number": "2024-CS-001",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "gender": "female",
    "date_of_birth": "2003-01-01",
    "department": "Computer Science",
    "batch_year": 2024,
}


def test_compile_mapping_matches_real_verified_output():
    transform_fn = ma.compile_mapping(APPROVED_STUDENT_MAPPING)
    result = transform_fn("student", SAMPLE_PIEAS_STUDENT)

    # Exactly the shape that was verified live against the real instance:
    # gr_no (not roll_number), gender translated to the single-letter code
    # op.student actually requires, department/batch_year dropped (not
    # guessed at), pieas_id preserved for the client's external-id handling.
    assert result == {
        "pieas_id": "PIEAS-STU-1",
        "gr_no": "2024-CS-001",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "gender": "f",
        "birth_date": "2003-01-01",
    }
    assert "department" not in result
    assert "batch_year" not in result


def test_compile_mapping_value_map_passes_through_unknown_values():
    """A value not in the map (e.g. a third gender value PIEAS might one
    day send) passes through unchanged rather than being silently dropped
    or defaulted -- fails visibly at the real Odoo write instead of being
    masked here.
    """
    transform_fn = ma.compile_mapping(APPROVED_STUDENT_MAPPING)
    record = dict(SAMPLE_PIEAS_STUDENT, gender="nonbinary")
    result = transform_fn("student", record)
    assert result["gender"] == "nonbinary"


def test_save_and_load_mapping_roundtrip(tmp_path):
    from openedu_orchestrator.models import FieldMappingEntry, MappingProposal

    proposal = MappingProposal(
        entity_type="course",
        target_model="op.course",
        field_mappings=[FieldMappingEntry(source_field="code", handling="direct")],
    )
    path = tmp_path / "mappings" / "course_pieas.json"
    ma.save_mapping(proposal, path)

    loaded = ma.load_mapping(path)
    assert loaded["entity_type"] == "course"
    assert loaded["field_mappings"][0]["source_field"] == "code"

    # And it's valid JSON a human could actually review/edit by hand.
    raw = json.loads(path.read_text())
    assert raw["target_model"] == "op.course"
