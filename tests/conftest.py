"""Shared test fixtures.

Nothing in this suite needs MySQL, Odoo or Gemini. The source database and the
ERP are replaced by small in-memory fakes that expose the same methods the
agents call, and the LLM is replaced by a scripted function.
"""
from __future__ import annotations

import copy
import re

import pytest

import agents
import config
import state


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Give every test its own state database and plan cache."""
    monkeypatch.setattr(config, "STATE_DB", str(tmp_path / "sync_state.db"))
    monkeypatch.setattr(config, "PLAN_CACHE", str(tmp_path / "plans"))
    state.init()


class LLMRecorder:
    """Stands in for `agents.ask_json`. Records prompts; answers from a script."""

    def __init__(self):
        self.prompts: list[str] = []
        self.responder = None          # callable(prompt) -> value, or None

    def __call__(self, prompt, attempts=4):
        self.prompts.append(prompt)
        if self.responder is None:
            raise agents.LLMUnavailable("no LLM in tests")
        return self.responder(prompt)


@pytest.fixture
def llm(monkeypatch):
    """By default the LLM is unreachable; set `llm.responder` to script it."""
    recorder = LLMRecorder()
    monkeypatch.setattr(agents, "ask_json", recorder)
    return recorder


class FakePieas:
    """In-memory stand-in for connectors.PieasDB.

    Understands exactly the query shapes the pipeline issues: the two COUNT
    queries of the Orchestrator's survey and the three extraction shapes
    (all rows, ids only, rows after a watermark).
    """

    def __init__(self, tables: dict[str, list[dict]], schemas: dict[str, dict]):
        self.tables = tables
        self.schemas = schemas
        self.queries: list[tuple[str, tuple]] = []

    def schema(self, table: str) -> dict:
        return copy.deepcopy(self.schemas[table])

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        self.queries.append((sql, params))
        table = re.search(r"\bfrom\s+`?(\w+)`?", sql, re.I).group(1)
        rows = self.tables[table]
        if "%s" in sql:
            rows = [r for r in rows if str(r["last_updated"]) > str(params[0])]
        low = sql.lower()
        if "count(*)" in low:
            return [{"n": len(rows)}]
        if re.match(r"\s*select\s+`?id`?\s+from", low):
            return [{"id": r["id"]} for r in rows]
        key = "last_updated" if "order by last_updated" in low else "id"
        return [dict(r) for r in sorted(rows, key=lambda r: r[key])]


class FakeOdoo:
    """In-memory stand-in for connectors.OdooRPC."""

    def __init__(self, fields: dict[str, dict], no_active_field: tuple[str, ...] = ()):
        self.fields = fields
        self.no_active_field = set(no_active_field)
        self.records: dict[tuple[str, int], dict] = {}
        self.calls: list[tuple] = []
        self.reject_field: dict[str, str] = {}   # field name -> error message
        self.fail_archive = False
        self._next_id = 100

    def writable_fields(self, model: str) -> dict:
        return copy.deepcopy(self.fields[model])

    def _check(self, vals: dict):
        for field, message in self.reject_field.items():
            if field in vals:
                raise RuntimeError(message)

    def create(self, model: str, vals: dict) -> int:
        self.calls.append(("create", model, dict(vals)))
        self._check(vals)
        self._next_id += 1
        self.records[(model, self._next_id)] = {"active": True, **vals}
        return self._next_id

    def write(self, model: str, odoo_id: int, vals: dict) -> bool:
        self.calls.append(("write", model, odoo_id, dict(vals)))
        self._check(vals)
        self.records[(model, odoo_id)].update(vals)
        return True

    def archive(self, model: str, odoo_id: int) -> str:
        self.calls.append(("archive", model, odoo_id))
        if self.fail_archive:
            raise RuntimeError("archive blocked by a dependent record")
        if model in self.no_active_field:
            del self.records[(model, odoo_id)]
            return "deleted"
        self.records[(model, odoo_id)]["active"] = False
        return "archived"

    def exists(self, model: str, odoo_id: int) -> bool:
        return (model, odoo_id) in self.records

    def writes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("create", "write", "archive")]


def column(name: str, column_type: str = "varchar(100)") -> dict:
    return {"column_name": name, "column_type": column_type}


STUDENT_SCHEMA = {
    "table": "students",
    "columns": [column("id", "int"), column("reg_no"), column("first_name"),
                column("last_name"), column("gender", "char(1)"),
                column("department_id", "int"), column("last_updated", "datetime")],
    "foreign_keys": {"department_id": "departments"},
}

DEPARTMENT_SCHEMA = {
    "table": "departments",
    "columns": [column("id", "int"), column("name"), column("code", "varchar(10)"),
                column("last_updated", "datetime")],
    "foreign_keys": {},
}

STUDENT_FIELDS = {
    "gr_no":         {"type": "char", "required": False},
    "first_name":    {"type": "char", "required": True},
    "last_name":     {"type": "char", "required": True},
    "name":          {"type": "char", "required": True},
    "gender":        {"type": "selection", "required": True,
                      "selection": [["m", "Male"], ["f", "Female"]]},
    "department_id": {"type": "many2one", "required": False, "relation": "op.department"},
    "partner_id":    {"type": "many2one", "required": True, "relation": "res.partner"},
}

DEPARTMENT_FIELDS = {
    "name": {"type": "char", "required": True},
    "code": {"type": "char", "required": True},
}


@pytest.fixture
def student_schema():
    return copy.deepcopy(STUDENT_SCHEMA)


@pytest.fixture
def student_fields():
    return copy.deepcopy(STUDENT_FIELDS)
