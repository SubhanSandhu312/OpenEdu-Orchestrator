"""Mock OpenEduCat (Odoo) client.

Odoo's real external API is XML-RPC: authenticate once against `common` to get
a uid, then call `object.execute_kw(db, uid, password, model, method, args)`
for `create` / `write` / `search_read`, etc. This class exposes exactly that
method surface (`create`, `write`, `search_read`, `read`) so the Loader Agent's
code does not need to change when it is later pointed at a real Odoo instance
via `xmlrpc.client.ServerProxy` -- only this class's internals would be
swapped for real XML-RPC calls.

Underneath, for this test build, it is backed by SQLite tables named after
Odoo's own convention (`op.student` -> `op_student`), with `active` and
`write_date` columns exactly as real Odoo models have. Only the Loader Agent
(and the read-only Validation Agent) should talk to this client -- nothing
else in the pipeline is allowed a connection to this database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openedu_orchestrator.config import OPENEDUCAT_DB_PATH

_MODEL_TABLE = {
    "op.student": "op_student",
    "op.faculty": "op_faculty",
    "op.course": "op_course",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS op_student (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pieas_id      TEXT UNIQUE,
    roll_number   TEXT,
    first_name    TEXT,
    last_name     TEXT,
    name          TEXT,
    email         TEXT,
    gender        TEXT,
    birth_date    TEXT,
    department    TEXT,
    batch_year    INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    create_date   TEXT NOT NULL,
    write_date    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_faculty (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pieas_id      TEXT UNIQUE,
    employee_code TEXT,
    first_name    TEXT,
    last_name     TEXT,
    name          TEXT,
    email         TEXT,
    department    TEXT,
    designation   TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    create_date   TEXT NOT NULL,
    write_date    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_course (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pieas_id      TEXT UNIQUE,
    code          TEXT,
    name          TEXT,
    department    TEXT,
    credit_hours  INTEGER,
    semester      TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    create_date   TEXT NOT NULL,
    write_date    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OpenEduCatClient:
    """ORM-shaped access to the mock OpenEduCat database.

    Method names (`create`, `write`, `search_read`) deliberately match Odoo's
    XML-RPC ORM surface.
    """

    def __init__(self, db_path: Path = OPENEDUCAT_DB_PATH):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _table(model: str) -> str:
        if model not in _MODEL_TABLE:
            raise ValueError(f"Unknown OpenEduCat model: {model}")
        return _MODEL_TABLE[model]

    def create(self, model: str, values: dict[str, Any]) -> int:
        table = self._table(model)
        cols = list(values.keys()) + ["active", "create_date", "write_date"]
        now = _now()
        params = list(values.values()) + [1, now, now]
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        cur = self._conn.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", params
        )
        self._conn.commit()
        return cur.lastrowid

    def write(self, model: str, record_id: int, values: dict[str, Any]) -> bool:
        table = self._table(model)
        if not values:
            return True
        set_cols = list(values.keys()) + ["write_date"]
        set_clause = ", ".join(f"{c} = ?" for c in set_cols)
        params = list(values.values()) + [_now(), record_id]
        cur = self._conn.execute(
            f"UPDATE {table} SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        return cur.rowcount > 0

    def archive(self, model: str, record_id: int) -> bool:
        """write(model, id, {'active': False}) -- the report's adopted deletion handling."""
        return self.write(model, record_id, {"active": False})

    def search_read(
        self,
        model: str,
        domain: Optional[list[tuple]] = None,
        fields: Optional[list[str]] = None,
    ) -> list[dict]:
        table = self._table(model)
        where_sql, params = self._domain_to_sql(domain or [])
        col_list = ", ".join(fields) if fields else "*"
        query = f"SELECT {col_list} FROM {table}"
        if where_sql:
            query += f" WHERE {where_sql}"
        cur = self._conn.execute(query, params)
        return [dict(row) for row in cur.fetchall()]

    def read(self, model: str, ids: list[int], fields: Optional[list[str]] = None) -> list[dict]:
        if not ids:
            return []
        table = self._table(model)
        col_list = ", ".join(fields) if fields else "*"
        placeholders = ", ".join("?" for _ in ids)
        cur = self._conn.execute(
            f"SELECT {col_list} FROM {table} WHERE id IN ({placeholders})", ids
        )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _domain_to_sql(domain: list[tuple]) -> tuple[str, list]:
        """Minimal Odoo-style domain support: a flat AND of equality tuples,
        e.g. [("pieas_id", "=", "PIEAS-STU-0001"), ("active", "=", True)].
        Enough for this pipeline's needs; not a general Odoo domain evaluator.
        """
        clauses, params = [], []
        for field, op, value in domain:
            if op != "=":
                raise NotImplementedError(f"Mock domain only supports '=', got {op!r}")
            clauses.append(f"{field} = ?")
            params.append(int(value) if isinstance(value, bool) else value)
        return " AND ".join(clauses), params

    def reset(self) -> None:
        for table in _MODEL_TABLE.values():
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()


def reset_database(db_path: Path = OPENEDUCAT_DB_PATH) -> OpenEduCatClient:
    if db_path.exists():
        db_path.unlink()
    return OpenEduCatClient(db_path)
