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


def test_cleared_field_written_as_none_reads_back_as_false_is_not_a_mismatch(dbs):
    """OdooXmlRpcClient marshals None -> False (XML-RPC cannot send None, and
    False is Odoo's own empty value), so a cleared field legitimately reads
    back as False. Without this the fix for field-clearing would make every
    such write report a spurious validation issue.
    """
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    assert validator._matches(None, False) is True


def test_none_still_matches_none():
    assert ValidationAgent._matches(None, None) is True


def test_none_expected_does_not_excuse_a_real_value():
    """None/False equivalence must not degrade into "None matches anything"."""
    assert ValidationAgent._matches(None, "Richard") is False


def test_real_mismatch_still_flagged_after_none_false_allowance(dbs):
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-N", "name": "N"}, "PIEAS-STU-N")
    issue = validator.validate_write("student", result.openeducat_id, {"name": "Wrong"})
    assert issue is not None and "Wrong" in issue
