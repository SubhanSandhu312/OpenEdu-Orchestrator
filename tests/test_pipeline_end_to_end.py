"""The whole LangGraph pipeline, run through its real entry point against fakes.

One scenario walks the three phases the README describes: bulk migration, the
fast pulse (inserts and updates via the watermark), and the slow pulse
(deletions, archived instead of destroyed), plus a record that comes back.
"""
import re

import pytest

import agents
import config
import graph
import state
from conftest import (FakeOdoo, FakePieas, DEPARTMENT_FIELDS, DEPARTMENT_SCHEMA,
                      STUDENT_FIELDS, STUDENT_SCHEMA)

MAPPING_PLANS = {
    "departments": {
        "fields": {"name": {"target": "name"}, "code": {"target": "code"}},
    },
    "students": {
        "fields": {
            "reg_no":     {"target": "gr_no"},
            "first_name": {"target": "first_name"},
            "last_name":  {"target": "last_name"},
            "gender":     {"target": "gender", "values": {"M": "m", "F": "f"}},
            "nickname":   {"target": "nick"},                 # invented: must be dropped
        },
        "computed": {"name": "{first_name} {last_name}"},
        "defaults": {"partner_id": 1},                         # must be dropped
        # "relations" left out on purpose: the FK must be recovered from the schema
    },
}


def student(pieas_id, first, last, gender, dept, updated):
    return {"id": pieas_id, "reg_no": f"PIEAS-25-{pieas_id:03d}", "first_name": first,
            "last_name": last, "gender": gender, "department_id": dept,
            "last_updated": updated}


@pytest.fixture
def world(monkeypatch, llm):
    monkeypatch.setattr(config, "ENTITIES", [("departments", "op.department"),
                                             ("students", "op.student")])
    monkeypatch.setattr(config, "ALLOWED_TABLES", {"departments", "students"})

    pieas = FakePieas(
        tables={
            "departments": [
                {"id": 1, "name": "Electrical Engineering", "code": "EE",
                 "last_updated": "2026-08-01 08:00:00"},
                {"id": 2, "name": "Computer Science", "code": "CS",
                 "last_updated": "2026-08-01 08:00:00"},
            ],
            "students": [
                student(1, "Ayesha", "Malik", "F", 1, "2026-08-01 09:00:00"),
                student(2, "Bilal", "Khan", "M", 2, "2026-08-01 09:05:00"),
                student(3, "Sana", "Iqbal", "F", 2, "2026-08-01 09:10:00"),
            ],
        },
        schemas={"departments": DEPARTMENT_SCHEMA, "students": STUDENT_SCHEMA},
    )
    odoo = FakeOdoo({"op.department": DEPARTMENT_FIELDS, "op.student": STUDENT_FIELDS})

    def scripted(prompt):
        # Only the Transformer's mapping request gets an answer. The Orchestrator
        # and Extractor are left to their deterministic fallbacks.
        table = re.search(r"SOURCE table `(\w+)`", prompt)
        if table:
            return MAPPING_PLANS[table.group(1)]
        raise agents.LLMUnavailable("not scripted")

    llm.responder = scripted
    monkeypatch.setattr(graph, "PieasDB", lambda: pieas)
    monkeypatch.setattr(graph, "OdooRPC", lambda: odoo)
    return pieas, odoo


def odoo_student(odoo, pieas_id):
    return odoo.records[("op.student", state.lookup_odoo_id("students", pieas_id))]


def test_bulk_then_incremental_then_deletion_then_revival(world):
    pieas, odoo = world

    # ── Phase 1: bulk migration, parents before children ────────────────────
    totals = graph.run("bulk")
    assert totals == {"created": 5, "updated": 0, "archived": 0, "deleted": 0, "failed": 0}

    ayesha = odoo_student(odoo, 1)
    assert ayesha["name"] == "Ayesha Malik"                    # computed field
    assert ayesha["gender"] == "f"                             # selection value mapped
    assert ayesha["department_id"] == state.lookup_odoo_id("departments", 1)
    assert "partner_id" not in ayesha and "nick" not in ayesha  # invented parts dropped
    assert state.get_watermark("students") == "2026-08-01 09:10:00"

    # ── Phase 2: nothing changed, so the fast pulse must write nothing ──────
    writes_before = len(odoo.writes())
    totals = graph.run("incremental")
    assert sum(totals.values()) == 0
    assert len(odoo.writes()) == writes_before

    # ── Phase 2: one update, one insert, one touch without a real change ────
    pieas.tables["students"][1].update(last_name="Ahmed", last_updated="2026-08-02 10:00:00")
    pieas.tables["students"][0].update(last_updated="2026-08-02 10:01:00")   # touched only
    pieas.tables["students"].append(student(4, "Daniyal", "Chaudhry", "M", 1,
                                            "2026-08-02 10:02:00"))
    totals = graph.run("incremental")
    assert (totals["created"], totals["updated"], totals["failed"]) == (1, 1, 0)
    assert odoo_student(odoo, 2)["name"] == "Bilal Ahmed"
    assert state.get_watermark("students") == "2026-08-02 10:02:00"

    # ── Phase 3: a row vanishes from the source; the ERP record is archived ─
    pieas.tables["students"] = [r for r in pieas.tables["students"] if r["id"] != 3]
    totals = graph.run("deletions")
    assert (totals["archived"], totals["deleted"], totals["failed"]) == (1, 0, 0)
    assert odoo_student(odoo, 3)["active"] is False
    assert odoo_student(odoo, 3)["name"] == "Sana Iqbal"       # history preserved
    assert state.lookup("students", 3)["status"] == "archived"

    # A second scan finds nothing new to retire.
    assert sum(graph.run("deletions").values()) == 0

    # ── The row comes back: the same ERP record is revived, not duplicated ──
    creates_before = len([c for c in odoo.calls if c[0] == "create"])
    pieas.tables["students"].append(student(3, "Sana", "Iqbal", "F", 2, "2026-08-03 08:00:00"))
    totals = graph.run("incremental")
    assert (totals["created"], totals["updated"]) == (0, 1)
    assert odoo_student(odoo, 3)["active"] is True
    assert len([c for c in odoo.calls if c[0] == "create"]) == creates_before
    assert state.lookup("students", 3)["status"] == "active"


def test_a_child_synced_before_its_parent_is_written_without_the_link(world):
    pieas, odoo = world
    pieas.tables["students"] = [student(1, "Ayesha", "Malik", "F", 99, "2026-08-01 09:00:00")]

    totals = graph.run("bulk")

    assert totals["failed"] == 0
    assert "department_id" not in odoo_student(odoo, 1)


def test_an_entity_with_no_usable_mapping_is_skipped_not_half_written(world, llm):
    pieas, odoo = world
    llm.responder = None                                       # Gemini fully down

    totals = graph.run("bulk")

    assert totals == {"created": 0, "updated": 0, "archived": 0, "deleted": 0, "failed": 0}
    assert odoo.records == {}
    assert state.is_empty("students")
