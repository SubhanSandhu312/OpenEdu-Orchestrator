"""Tests for the multi-source-system generalization: source_adapter.py's
formal contract, source_registry.py's lookup, and example_univ_source.py
as a second, genuinely different adapter proving the abstraction isn't
PIEAS-specific.
"""

from __future__ import annotations

import pytest

from openedu_orchestrator import example_univ_source, pieas_source
from openedu_orchestrator import mapping_authoring as ma
from openedu_orchestrator.models import ExampleUnivStudent
from openedu_orchestrator.source_adapter import SourceAdapter, check_adapter
from openedu_orchestrator.source_registry import SOURCE_SYSTEMS, get_source_system


def test_pieas_source_satisfies_contract():
    check_adapter(pieas_source)
    assert isinstance(pieas_source, SourceAdapter)


def test_example_univ_source_satisfies_contract():
    check_adapter(example_univ_source)
    assert isinstance(example_univ_source, SourceAdapter)


def test_check_adapter_names_the_missing_functions():
    class Incomplete:
        __name__ = "incomplete_module"

        @staticmethod
        def get_connection(conn_info=None):
            return None

    with pytest.raises(TypeError, match="fetch_changed.*fetch_page.*fetch_ids|fetch_ids.*fetch_page.*fetch_changed"):
        check_adapter(Incomplete)


def test_registry_has_both_source_systems():
    assert set(SOURCE_SYSTEMS) == {"pieas", "example_univ"}


def test_get_source_system_unknown_raises():
    with pytest.raises(ValueError, match="Unknown source system"):
        get_source_system("nonexistent_university")


def test_example_univ_adapter_crud(tmp_path):
    db_path = tmp_path / "example_univ.db"
    conn = example_univ_source.reset_database(db_path)
    student = ExampleUnivStudent(
        student_ref="EXU-00001", given_name="Ada", family_name="Lovelace",
        contact_email="ada@example.com", sex="F", dob="2003-01-01",
        major="Computer Science", intake_year=2024,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    example_univ_source.insert_student(conn, student)

    assert example_univ_source.count_rows(conn, "students") == 1
    row = example_univ_source.row_by_id(conn, "students", "EXU-00001")
    # Known naming debt, deliberately: student_ref is aliased to pieas_id
    # in every query so the rest of the pipeline (which hardcodes that
    # key) works unmodified -- see example_univ_source.py's docstring.
    assert row["pieas_id"] == "EXU-00001"
    assert row["given_name"] == "Ada"

    example_univ_source.update_fields(conn, "students", "EXU-00001", {"major": "Physics"})
    assert example_univ_source.row_by_id(conn, "students", "EXU-00001")["major"] == "Physics"

    example_univ_source.delete_row(conn, "students", "EXU-00001")
    assert example_univ_source.row_by_id(conn, "students", "EXU-00001") is None


def test_extract_source_schema_defaults_to_pieas():
    schema = ma.extract_source_schema("student")
    assert "roll_number" in schema  # PIEAS-shaped, the default/backward-compat path


def test_extract_source_schema_for_example_univ():
    schema = ma.extract_source_schema("student", source_system="example_univ")
    assert "given_name" in schema
    assert "roll_number" not in schema  # confirms this is genuinely a different schema, not PIEAS's


def test_extract_source_schema_unknown_entity_for_source():
    with pytest.raises(ValueError, match="entity_type"):
        ma.extract_source_schema("faculty", source_system="example_univ")  # only student is registered
