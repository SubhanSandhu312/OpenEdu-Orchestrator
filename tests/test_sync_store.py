from __future__ import annotations

from datetime import datetime, timezone

from openedu_orchestrator import sync_store as store

SRC = "pieas"


def _conn(dbs):
    return store.get_connection(dbs.sync_path)


def test_is_bulk_mode_true_when_empty(dbs):
    conn = _conn(dbs)
    assert store.is_bulk_mode(conn, SRC, "student") is True


def test_upsert_mapping_then_lookup(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, SRC, "PIEAS-STU-1", 101, "student", "hash1")
    row = store.get_mapping(conn, SRC, "PIEAS-STU-1", "student")
    assert row["openeducat_id"] == 101
    assert row["content_hash"] == "hash1"
    assert store.is_bulk_mode(conn, SRC, "student") is False


def test_upsert_mapping_on_conflict_updates_not_duplicates(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, SRC, "PIEAS-STU-1", 101, "student", "hash1")
    store.upsert_mapping(conn, SRC, "PIEAS-STU-1", 101, "student", "hash2")
    assert store.count_mappings(conn, SRC, "student") == 1
    row = store.get_mapping(conn, SRC, "PIEAS-STU-1", "student")
    assert row["content_hash"] == "hash2"


def test_mapping_scoped_by_entity_type(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, SRC, "SAME-ID", 1, "student", "h")
    store.upsert_mapping(conn, SRC, "SAME-ID", 2, "faculty", "h")
    assert store.count_mappings(conn, SRC, "student") == 1
    assert store.count_mappings(conn, SRC, "faculty") == 1
    assert store.get_mapping(conn, SRC, "SAME-ID", "student")["openeducat_id"] == 1
    assert store.get_mapping(conn, SRC, "SAME-ID", "faculty")["openeducat_id"] == 2


def test_mapping_scoped_by_source_system(dbs):
    """The actual point of adding source_system to the schema: two
    different source systems can map the same source_id under the same
    entity_type without colliding -- each gets its own row.
    """
    conn = _conn(dbs)
    store.upsert_mapping(conn, "pieas", "SAME-ID", 1, "student", "h")
    store.upsert_mapping(conn, "example_univ", "SAME-ID", 2, "student", "h")
    assert store.count_mappings(conn, "pieas", "student") == 1
    assert store.count_mappings(conn, "example_univ", "student") == 1
    assert store.get_mapping(conn, "pieas", "SAME-ID", "student")["openeducat_id"] == 1
    assert store.get_mapping(conn, "example_univ", "SAME-ID", "student")["openeducat_id"] == 2


def test_watermark_advances_and_reads_back(dbs):
    conn = _conn(dbs)
    assert store.get_watermark(conn, SRC, "student") is None
    ts = datetime.now(timezone.utc)
    store.advance_watermark(conn, SRC, "student", ts)
    assert store.get_watermark(conn, SRC, "student") == ts


def test_watermark_scoped_by_source_system(dbs):
    """Two source systems syncing the same entity_type must not share a
    watermark -- before source_system was added to sync_state's key, they
    would have fought over the same row.
    """
    conn = _conn(dbs)
    ts1 = datetime.now(timezone.utc)
    store.advance_watermark(conn, "pieas", "student", ts1)
    assert store.get_watermark(conn, "example_univ", "student") is None
    assert store.get_watermark(conn, "pieas", "student") == ts1


def test_content_hash_stable_and_order_independent(dbs):
    h1 = store.content_hash({"a": 1, "b": 2})
    h2 = store.content_hash({"b": 2, "a": 1})
    h3 = store.content_hash({"a": 1, "b": 3})
    assert h1 == h2
    assert h1 != h3


def test_touch_mapping_bumps_timestamp_without_changing_target(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, SRC, "PIEAS-STU-1", 101, "student", "hash1")
    before = store.get_mapping(conn, SRC, "PIEAS-STU-1", "student")["last_synced_at"]
    store.touch_mapping(conn, SRC, "PIEAS-STU-1", "student")
    after = store.get_mapping(conn, SRC, "PIEAS-STU-1", "student")
    assert after["openeducat_id"] == 101
    assert after["last_synced_at"] >= before
