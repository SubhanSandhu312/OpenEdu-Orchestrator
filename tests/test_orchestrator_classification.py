from __future__ import annotations

from openedu_orchestrator import sync_store as store

SRC = "pieas"  # matches the `agents` fixture's default OrchestratorAgent(source_system="pieas")


def _record(source_id="PIEAS-STU-1", email="a@example.com", last_updated="2026-01-01T00:00:00"):
    return {
        "source_id": source_id, "roll_number": "2024-CS-001", "first_name": "Ada",
        "last_name": "Lovelace", "email": email, "gender": "female",
        "date_of_birth": "2003-01-01", "department": "Computer Science",
        "batch_year": 2024, "last_updated": last_updated,
    }


def test_classify_unmapped_record_as_create(agents):
    classified = agents.orchestrator.classify_records("student", [_record()])
    assert classified[0]["action"] == "create"
    assert classified[0]["openeducat_id"] is None


def test_classify_mapped_record_with_same_hash_as_unchanged(agents):
    record = _record()
    business_fields = {k: v for k, v in record.items() if k != "last_updated"}
    h = store.content_hash(business_fields)
    store.upsert_mapping(agents.orchestrator._conn, SRC, record["source_id"], 42, "student", h)

    classified = agents.orchestrator.classify_records("student", [record])
    assert classified[0]["action"] == "unchanged"
    assert classified[0]["openeducat_id"] == 42


def test_classify_mapped_record_with_different_hash_as_update(agents):
    record = _record()
    business_fields = {k: v for k, v in record.items() if k != "last_updated"}
    h = store.content_hash(business_fields)
    store.upsert_mapping(agents.orchestrator._conn, SRC, record["source_id"], 42, "student", h)

    changed = _record(email="new-email@example.com")
    classified = agents.orchestrator.classify_records("student", [changed])
    assert classified[0]["action"] == "update"
    assert classified[0]["openeducat_id"] == 42


def test_classify_deletions_finds_missing_mapping(agents):
    conn = agents.orchestrator._conn
    store.upsert_mapping(conn, SRC, "PIEAS-STU-1", 1, "student", "h1")
    store.upsert_mapping(conn, SRC, "PIEAS-STU-2", 2, "student", "h2")
    store.upsert_mapping(conn, SRC, "PIEAS-STU-3", 3, "student", "h3")

    # PIEAS-STU-2 no longer exists on the source
    fetched_ids = ["PIEAS-STU-1", "PIEAS-STU-3"]
    candidates = agents.orchestrator.classify_deletions("student", fetched_ids)

    assert len(candidates) == 1
    assert candidates[0]["source_id"] == "PIEAS-STU-2"
    assert candidates[0]["openeducat_id"] == 2


def test_classify_deletions_scoped_to_entity_type(agents):
    conn = agents.orchestrator._conn
    store.upsert_mapping(conn, SRC, "SAME-ID", 1, "student", "h")
    store.upsert_mapping(conn, SRC, "SAME-ID", 2, "faculty", "h")

    # "SAME-ID" is missing from students' fetched ids but faculty wasn't even checked
    candidates = agents.orchestrator.classify_deletions("student", [])
    assert len(candidates) == 1
    assert candidates[0]["entity_type"] == "student"


def test_is_bulk_mode_flips_after_first_mapping(agents):
    assert agents.orchestrator.is_bulk_mode("student") is True
    store.upsert_mapping(agents.orchestrator._conn, SRC, "PIEAS-STU-1", 1, "student", "h")
    assert agents.orchestrator.is_bulk_mode("student") is False
    # unrelated entity type is unaffected
    assert agents.orchestrator.is_bulk_mode("faculty") is True


def test_archived_mapping_classifies_as_update_even_when_hash_matches(dbs, agents):
    """The archived check must come *before* the content-hash shortcut."""
    from openedu_orchestrator import sync_store as store

    record = {"source_id": "PIEAS-STU-R1", "name": "R", "last_updated": "2026-01-01T00:00:00"}
    business = {k: v for k, v in record.items() if k != "last_updated"}
    store.upsert_mapping(
        agents.orchestrator._conn, source_system="pieas", source_id="PIEAS-STU-R1",
        openeducat_id=99, entity_type="student", hash_=store.content_hash(business),
    )
    # identical content -> would normally be "unchanged"
    assert agents.orchestrator.classify_records("student", [record])[0]["action"] == "unchanged"

    store.mark_archived(agents.orchestrator._conn, "pieas", "PIEAS-STU-R1", "student")
    revived = agents.orchestrator.classify_records("student", [record])[0]
    assert revived["action"] == "update"
    assert revived["openeducat_id"] == 99
