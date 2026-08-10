from __future__ import annotations

import time
from datetime import datetime

from openedu_orchestrator import pieas_source as src


def test_seed_populates_all_tables(dbs):
    assert src.count_rows(dbs.pieas_conn, "students") == 12
    assert src.count_rows(dbs.pieas_conn, "faculty") == 5
    assert src.count_rows(dbs.pieas_conn, "courses") == 4


def test_fetch_ids_returns_all_pieas_ids(dbs):
    ids = src.fetch_ids(dbs.pieas_conn, "students")
    assert len(ids) == 12
    assert all(i.startswith("PIEAS-STU-") for i in ids)


def test_fetch_page_paginates_in_stable_order(dbs):
    page1 = src.fetch_page(dbs.pieas_conn, "students", limit=5, offset=0)
    page2 = src.fetch_page(dbs.pieas_conn, "students", limit=5, offset=5)
    page3 = src.fetch_page(dbs.pieas_conn, "students", limit=5, offset=10)
    assert len(page1) == 5 and len(page2) == 5 and len(page3) == 2
    all_ids = [r["source_id"] for r in (*page1, *page2, *page3)]
    assert len(set(all_ids)) == 12  # no overlap, none skipped


def test_fetch_changed_respects_watermark(dbs):
    all_rows = src.fetch_changed(dbs.pieas_conn, "students", None)
    assert len(all_rows) == 12

    watermark = datetime.fromisoformat(all_rows[len(all_rows) // 2]["last_updated"])
    newer = src.fetch_changed(dbs.pieas_conn, "students", watermark)
    assert all(datetime.fromisoformat(r["last_updated"]) > watermark for r in newer)
    assert len(newer) < len(all_rows)


def test_update_fields_bumps_last_updated_via_trigger(dbs):
    pieas_id = "PIEAS-STU-00001"
    before = src.row_by_id(dbs.pieas_conn, "students", pieas_id)["last_updated"]
    time.sleep(0.01)
    src.update_fields(dbs.pieas_conn, "students", pieas_id, {"department": "Physics"})
    after_row = src.row_by_id(dbs.pieas_conn, "students", pieas_id)
    assert after_row["department"] == "Physics"
    assert after_row["last_updated"] > before


def test_delete_row_actually_removes_it(dbs):
    pieas_id = "PIEAS-STU-00001"
    assert src.row_by_id(dbs.pieas_conn, "students", pieas_id) is not None
    src.delete_row(dbs.pieas_conn, "students", pieas_id)
    assert src.row_by_id(dbs.pieas_conn, "students", pieas_id) is None
    assert pieas_id not in src.fetch_ids(dbs.pieas_conn, "students")
