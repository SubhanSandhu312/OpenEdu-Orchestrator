from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


def test_target_guard_adopts_on_a_fresh_store(dbs):
    """Nothing synced yet means no ids exist to misinterpret, so claiming the
    target is safe and must not require ceremony.
    """
    from openedu_orchestrator.agents.orchestrator import OrchestratorAgent

    orch = OrchestratorAgent(dbs.sync_path, target="mock")
    orch.ensure_target("student")
    orch.ensure_target("student")  # idempotent
    orch.close()


def test_target_guard_blocks_switching_target(dbs):
    """The core protection: mock ids handed to a real Odoo would update
    whatever unrelated record happens to hold that id.
    """
    from openedu_orchestrator.agents.orchestrator import OrchestratorAgent
    from openedu_orchestrator.sync_store import TargetMismatchError

    orch = OrchestratorAgent(dbs.sync_path, target="mock")
    orch.ensure_target("student")
    orch.close()

    other = OrchestratorAgent(dbs.sync_path, target="real")
    with pytest.raises(TargetMismatchError, match="mock"):
        other.ensure_target("student")
    other.close()


def test_target_guard_refuses_to_guess_on_a_legacy_store(dbs):
    """A store written before target tracking has mappings but no recorded
    target. Adopting would be a guess with silent-corruption consequences.
    """
    from openedu_orchestrator.agents.orchestrator import OrchestratorAgent
    from openedu_orchestrator.sync_store import TargetMismatchError

    seeded = OrchestratorAgent(dbs.sync_path)  # target=None -> guard opted out
    seeded.record_load_results(
        "student",
        [{"source_id": "S1", "content_hash": "h"}],
        [{"source_id": "S1", "openeducat_id": 1, "ok": True}],
    )
    seeded.close()

    orch = OrchestratorAgent(dbs.sync_path, target="real")
    with pytest.raises(TargetMismatchError, match="predates target tracking"):
        orch.ensure_target("student")
    orch.close()


def test_target_guard_is_opt_out_by_default(dbs):
    """Every pre-existing caller and test constructs OrchestratorAgent with
    no target and must keep working unchanged.
    """
    from openedu_orchestrator.agents.orchestrator import OrchestratorAgent

    orch = OrchestratorAgent(dbs.sync_path)
    orch.ensure_target("student")
    orch.close()


def test_target_guard_is_per_entity(dbs):
    """Entities are migrated independently, so the guard must not force them
    all onto one target in lockstep.
    """
    from openedu_orchestrator.agents.orchestrator import OrchestratorAgent

    orch = OrchestratorAgent(dbs.sync_path, target="mock")
    orch.ensure_target("student")
    orch.ensure_target("faculty")
    orch.close()
