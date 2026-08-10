from __future__ import annotations

from openedu_orchestrator import pieas_source as src
from openedu_orchestrator.graph import run_cycle


def test_deletion_cycle_archives_removed_record(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    ids = src.fetch_ids(dbs.pieas_conn, "students")
    removed_id = ids[0]

    mapping_before = agents.orchestrator._conn.execute(
        "SELECT openeducat_id FROM sync_mapping WHERE source_id = ?", (removed_id,)
    ).fetchone()
    assert dbs.oc_client.read("op.student", [mapping_before["openeducat_id"]])[0]["active"] == 1

    src.delete_row(dbs.pieas_conn, "students", removed_id)
    report = run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader)

    assert report.fetched == 11  # 12 - 1 deleted
    assert report.archived == 1
    archived_row = dbs.oc_client.read("op.student", [mapping_before["openeducat_id"]])[0]
    assert archived_row["active"] == 0


def test_deletion_cycle_change_cycle_cannot_see_a_deletion(dbs, agents):
    """Structural check mirroring Section 5.3: a filtered last_updated query
    (the change cycle) must never notice an absence -- only the deletion
    cycle's unfiltered ID fetch can.
    """
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    ids = src.fetch_ids(dbs.pieas_conn, "students")
    src.delete_row(dbs.pieas_conn, "students", ids[0])

    change_report = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert change_report.fetched == 0  # deletion invisible to the change cycle
    assert agents.orchestrator.mapping_count("student") == 12  # mapping row still present, unarchived


def test_deletion_cycle_leaves_untouched_records_active(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    ids = src.fetch_ids(dbs.pieas_conn, "students")
    src.delete_row(dbs.pieas_conn, "students", ids[0])
    run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader)

    active = dbs.oc_client.search_read("op.student", [("active", "=", True)])
    assert len(active) == 11


def test_deletion_cycle_no_op_when_nothing_removed(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    report = run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert report.archived == 0
    assert report.fetched == 12
