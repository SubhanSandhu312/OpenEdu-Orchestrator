from __future__ import annotations

from openedu_orchestrator.agents.loader import LoaderAgent
from openedu_orchestrator.agents.validator import ValidationAgent


def test_validate_write_passes_when_fields_match(dbs):
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    issue = validator.validate_write("student", result.openeducat_id, {"pieas_id": "PIEAS-STU-X", "name": "X"})
    assert issue is None


def test_validate_write_flags_mismatch(dbs):
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    issue = validator.validate_write("student", result.openeducat_id, {"name": "Some Other Name"})
    assert issue is not None
    assert "Some Other Name" in issue


def test_validate_write_flags_missing_record(dbs):
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    issue = validator.validate_write("student", 999999, {"name": "X"})
    assert issue is not None
    assert "missing" in issue


def test_validate_archive_passes_when_inactive(dbs):
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    loader.apply("student", "archive", {}, "PIEAS-STU-X", result.openeducat_id)
    assert validator.validate_archive("student", result.openeducat_id) is None


def test_validate_archive_flags_still_active(dbs):
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    issue = validator.validate_archive("student", result.openeducat_id)
    assert issue is not None
