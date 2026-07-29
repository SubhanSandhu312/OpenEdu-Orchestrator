from __future__ import annotations

import pytest


def test_create_assigns_incrementing_id_and_defaults_active(dbs):
    client = dbs.oc_client
    id1 = client.create("op.student", {"pieas_id": "PIEAS-STU-A", "name": "A"})
    id2 = client.create("op.student", {"pieas_id": "PIEAS-STU-B", "name": "B"})
    assert id2 == id1 + 1
    rows = client.read("op.student", [id1])
    assert rows[0]["active"] == 1


def test_write_updates_fields_and_write_date(dbs):
    client = dbs.oc_client
    rid = client.create("op.faculty", {"pieas_id": "PIEAS-FAC-A", "name": "A", "designation": "Lecturer"})
    before = client.read("op.faculty", [rid])[0]
    client.write("op.faculty", rid, {"designation": "Professor"})
    after = client.read("op.faculty", [rid])[0]
    assert after["designation"] == "Professor"
    assert after["write_date"] >= before["write_date"]


def test_archive_sets_active_false(dbs):
    client = dbs.oc_client
    rid = client.create("op.course", {"pieas_id": "PIEAS-CRS-A", "name": "Intro"})
    client.archive("op.course", rid)
    row = client.read("op.course", [rid])[0]
    assert row["active"] == 0


def test_search_read_domain_filters_by_equality(dbs):
    client = dbs.oc_client
    rid = client.create("op.student", {"pieas_id": "PIEAS-STU-Z", "name": "Z"})
    client.archive("op.student", rid)
    active = client.search_read("op.student", [("active", "=", True)])
    archived = client.search_read("op.student", [("active", "=", False)])
    assert rid not in [r["id"] for r in active]
    assert rid in [r["id"] for r in archived]


def test_unknown_model_raises(dbs):
    with pytest.raises(ValueError):
        dbs.oc_client.create("op.nonexistent", {})
