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


def test_marshal_values_converts_none_to_false():
    """xmlrpc.client raises "cannot marshal None unless allow_none is enabled"
    before a request is even sent, so a source field cleared to NULL would
    otherwise fail the whole write instead of clearing the target field.
    False is Odoo's own canonical empty value.
    """
    from openedu_orchestrator.openeducat_client import OdooXmlRpcClient
    out = OdooXmlRpcClient._marshal_values({"first_name": None, "gr_no": "R1", "batch_year": 0})
    assert out == {"first_name": False, "gr_no": "R1", "batch_year": 0}


def test_marshal_values_leaves_falsy_non_none_values_alone():
    """0, "" and False are legitimate values, not "no value" -- only None is."""
    from openedu_orchestrator.openeducat_client import OdooXmlRpcClient
    out = OdooXmlRpcClient._marshal_values({"a": 0, "b": "", "c": False, "d": None})
    assert out == {"a": 0, "b": "", "c": False, "d": False}


def test_marshal_values_output_is_xmlrpc_serialisable():
    """The actual property that matters -- proven by round-tripping through
    xmlrpc's own marshaller rather than asserting on our own conversion.
    """
    import xmlrpc.client
    from openedu_orchestrator.openeducat_client import OdooXmlRpcClient
    with pytest.raises(TypeError, match="cannot marshal None"):
        xmlrpc.client.dumps(({"first_name": None},))
    xmlrpc.client.dumps((OdooXmlRpcClient._marshal_values({"first_name": None}),))
