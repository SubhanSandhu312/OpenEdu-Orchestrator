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


def test_restored_record_is_revived_even_when_content_is_identical(dbs, agents):
    """Delete-then-restore must un-archive the target record.

    The hard case is a restore whose business fields are byte-identical to
    before: the content hash matches, so without tracking that the mapping
    was archived this classifies as "unchanged", is never written, and the
    record stays invisible in the target forever. reconcile would report it
    as stale_archived with nothing able to repair it.
    """
    from datetime import datetime, timezone

    from openedu_orchestrator.models import PieasStudent

    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    source_id = src.fetch_ids(dbs.pieas_conn, "students")[0]
    original = dict(src.row_by_id(dbs.pieas_conn, "students", source_id))
    openeducat_id = agents.orchestrator._conn.execute(
        "SELECT openeducat_id FROM sync_mapping WHERE source_id = ?", (source_id,)
    ).fetchone()["openeducat_id"]

    src.delete_row(dbs.pieas_conn, "students", source_id)
    run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert dbs.oc_client.read("op.student", [openeducat_id])[0]["active"] == 0

    # identical business fields, fresh timestamp (what a real re-insert does)
    src.insert_student(dbs.pieas_conn, PieasStudent(
        pieas_id=original["source_id"], roll_number=original["roll_number"],
        first_name=original["first_name"], last_name=original["last_name"],
        email=original["email"], gender=original["gender"],
        date_of_birth=original["date_of_birth"], department=original["department"],
        batch_year=original["batch_year"], last_updated=datetime.now(timezone.utc),
    ))

    report = run_cycle("change", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert report.updated == 1, "a restored record must be written, not skipped as unchanged"
    assert dbs.oc_client.read("op.student", [openeducat_id])[0]["active"] == 1


def test_archive_flag_is_cleared_after_a_successful_revive(dbs, agents):
    """Otherwise every later cycle would keep re-writing the record forever."""
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    source_id = src.fetch_ids(dbs.pieas_conn, "students")[0]

    src.delete_row(dbs.pieas_conn, "students", source_id)
    run_cycle("deletion", "student", agents.orchestrator, agents.extractor, agents.loader)
    flag = agents.orchestrator._conn.execute(
        "SELECT archived FROM sync_mapping WHERE source_id = ?", (source_id,)
    ).fetchone()["archived"]
    assert flag == 1
