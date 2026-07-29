from __future__ import annotations

from openedu_orchestrator.agents.loader import LoaderAgent


def test_create_returns_ok_load_result(dbs):
    loader = LoaderAgent(dbs.oc_client)
    result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    assert result.ok is True
    assert result.action == "create"
    assert result.openeducat_id > 0


def test_update_writes_fields(dbs):
    loader = LoaderAgent(dbs.oc_client)
    create_result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    update_result = loader.apply(
        "student", "update", {"name": "X Updated"}, "PIEAS-STU-X", create_result.openeducat_id
    )
    assert update_result.ok is True
    row = loader.read_back("student", create_result.openeducat_id)
    assert row["name"] == "X Updated"


def test_archive_sets_inactive(dbs):
    loader = LoaderAgent(dbs.oc_client)
    create_result = loader.apply("student", "create", {"pieas_id": "PIEAS-STU-X", "name": "X"}, "PIEAS-STU-X")
    archive_result = loader.apply("student", "archive", {}, "PIEAS-STU-X", create_result.openeducat_id)
    assert archive_result.ok is True
    row = loader.read_back("student", create_result.openeducat_id)
    assert row["active"] == 0


def test_update_without_id_fails_gracefully_not_raises(dbs):
    loader = LoaderAgent(dbs.oc_client)
    result = loader.apply("student", "update", {"name": "X"}, "PIEAS-STU-X", None)
    assert result.ok is False
    assert result.error is not None


def test_apply_batch_preserves_order(dbs):
    loader = LoaderAgent(dbs.oc_client)
    items = [
        {"action": "create", "pieas_id": "PIEAS-STU-A", "fields": {"pieas_id": "PIEAS-STU-A", "name": "A"}},
        {"action": "create", "pieas_id": "PIEAS-STU-B", "fields": {"pieas_id": "PIEAS-STU-B", "name": "B"}},
    ]
    results = loader.apply_batch("student", items)
    assert [r.pieas_id for r in results] == ["PIEAS-STU-A", "PIEAS-STU-B"]
    assert all(r.ok for r in results)
