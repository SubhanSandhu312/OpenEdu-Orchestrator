"""The state store: watermarks, the id mapping, and change hashes."""
import state


def test_watermark_starts_at_the_epoch():
    assert state.get_watermark("students") == "1970-01-01 00:00:00"


def test_watermark_only_moves_forward():
    state.advance_watermark("students", "2026-08-10 09:00:00")
    state.advance_watermark("students", "2026-08-01 09:00:00")   # older: ignored
    state.advance_watermark("students", "")                       # empty: ignored
    assert state.get_watermark("students") == "2026-08-10 09:00:00"

    state.advance_watermark("students", "2026-08-11 00:00:00")
    assert state.get_watermark("students") == "2026-08-11 00:00:00"


def test_watermarks_are_tracked_per_entity():
    state.advance_watermark("students", "2026-08-10 09:00:00")
    assert state.get_watermark("faculty") == "1970-01-01 00:00:00"


def test_recording_a_deletion_scan_never_moves_the_watermark():
    state.advance_watermark("students", "2026-08-10 09:00:00")
    state.record_deletion_scan("students")
    state.record_deletion_scan("faculty")       # no watermark row yet
    assert state.get_watermark("students") == "2026-08-10 09:00:00"
    assert state.get_watermark("faculty") == "1970-01-01 00:00:00"


def test_mapping_round_trip_and_update():
    assert state.is_empty("students")
    state.upsert("students", 7, "op.student", 101, "aaaa")
    state.upsert("students", 7, "op.student", 101, "bbbb")      # same row, new hash

    row = state.lookup("students", 7)
    assert (row["odoo_id"], row["row_hash"], row["status"]) == (101, "bbbb", "active")
    assert state.lookup_odoo_id("students", 7) == 101
    assert state.count("students") == 1
    assert not state.is_empty("students")
    assert state.lookup("students", 8) is None


def test_archived_rows_leave_the_live_id_set_but_stay_in_the_mapping():
    state.upsert("students", 1, "op.student", 101, "h1")
    state.upsert("students", 2, "op.student", 102, "h2")
    state.mark_archived("students", 2)

    assert state.known_ids("students") == [1]
    assert state.lookup("students", 2)["status"] == "archived"
    assert state.count("students") == 2


def test_row_hash_ignores_key_order_and_detects_a_changed_value():
    a = state.row_hash({"name": "Ayesha", "gender": "f"})
    b = state.row_hash({"gender": "f", "name": "Ayesha"})
    c = state.row_hash({"name": "Ayesha", "gender": "m"})
    assert a == b
    assert a != c


def test_status_report_counts_active_and_archived():
    state.upsert("students", 1, "op.student", 101, "h1")
    state.upsert("students", 2, "op.student", 102, "h2")
    state.mark_archived("students", 2)

    report = {r["entity"]: r for r in state.status_report()}
    assert (report["students"]["active"], report["students"]["archived"]) == (1, 1)
    assert report["faculty"]["active"] == 0
