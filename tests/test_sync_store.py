from __future__ import annotations

from datetime import datetime, timezone

from openedu_orchestrator import sync_store as store


def _conn(dbs):
    return store.get_connection(dbs.sync_path)


def test_is_bulk_mode_true_when_empty(dbs):
    conn = _conn(dbs)
    assert store.is_bulk_mode(conn, "student") is True


def test_upsert_mapping_then_lookup(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, "PIEAS-STU-1", 101, "student", "hash1")
    row = store.get_mapping(conn, "PIEAS-STU-1", "student")
    assert row["openeducat_id"] == 101
    assert row["content_hash"] == "hash1"
    assert store.is_bulk_mode(conn, "student") is False


def test_upsert_mapping_on_conflict_updates_not_duplicates(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, "PIEAS-STU-1", 101, "student", "hash1")
    store.upsert_mapping(conn, "PIEAS-STU-1", 101, "student", "hash2")
    assert store.count_mappings(conn, "student") == 1
    row = store.get_mapping(conn, "PIEAS-STU-1", "student")
    assert row["content_hash"] == "hash2"


def test_mapping_scoped_by_entity_type(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, "SAME-ID", 1, "student", "h")
    store.upsert_mapping(conn, "SAME-ID", 2, "faculty", "h")
    assert store.count_mappings(conn, "student") == 1
    assert store.count_mappings(conn, "faculty") == 1
    assert store.get_mapping(conn, "SAME-ID", "student")["openeducat_id"] == 1
    assert store.get_mapping(conn, "SAME-ID", "faculty")["openeducat_id"] == 2


def test_watermark_advances_and_reads_back(dbs):
    conn = _conn(dbs)
    assert store.get_watermark(conn, "student") is None
    ts = datetime.now(timezone.utc)
    store.advance_watermark(conn, "student", ts)
    assert store.get_watermark(conn, "student") == ts


def test_content_hash_stable_and_order_independent(dbs):
    h1 = store.content_hash({"a": 1, "b": 2})
    h2 = store.content_hash({"b": 2, "a": 1})
    h3 = store.content_hash({"a": 1, "b": 3})
    assert h1 == h2
    assert h1 != h3


def test_touch_mapping_bumps_timestamp_without_changing_target(dbs):
    conn = _conn(dbs)
    store.upsert_mapping(conn, "PIEAS-STU-1", 101, "student", "hash1")
    before = store.get_mapping(conn, "PIEAS-STU-1", "student")["last_synced_at"]
    store.touch_mapping(conn, "PIEAS-STU-1", "student")
    after = store.get_mapping(conn, "PIEAS-STU-1", "student")
    assert after["openeducat_id"] == 101
    assert after["last_synced_at"] >= before
