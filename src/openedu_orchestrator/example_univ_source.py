"""A second, genuinely different source-system adapter -- proves the
SourceAdapter contract (source_adapter.py) actually generalizes, not just
"works for PIEAS." Student-only (a full second university mirroring all
three entity types isn't needed to prove the abstraction; the interesting
part is the *shape* of the schema, and one entity type demonstrates that).

Deliberately different from pieas_source.py in field names AND value
conventions (see models.ExampleUnivStudent's docstring) -- if the
mapping-authoring tool and the pipeline both handle this without any
PIEAS-specific assumption leaking through, the abstraction is real.

Every query aliases its own primary key (student_ref) and watermark
column (updated_at) to the pipeline's two generic wire-format keys,
source_id and last_updated -- the same convention pieas_source.py and
pieas_source_mysql.py follow, so orchestrator.py/sync_store.py/graph.py
have no source-specific assumption left anywhere in them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from openedu_orchestrator.models import ExampleUnivStudent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_ref   TEXT PRIMARY KEY,
    given_name    TEXT NOT NULL,
    family_name   TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    sex           TEXT NOT NULL,
    dob           TEXT NOT NULL,
    major         TEXT NOT NULL,
    intake_year   INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_students_updated_at ON students(updated_at);
"""

_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_students_touch
AFTER UPDATE ON students
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE students SET updated_at = strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
    WHERE student_ref = NEW.student_ref;
END;
"""

# Every SELECT aliases the primary key and the watermark timestamp column
# to the pipeline's two generic wire-format keys (source_id, last_updated)
# -- see module docstring. Every other field keeps its own natural name.
_SELECT_COLUMNS = (
    "student_ref AS source_id, given_name, family_name, contact_email, "
    "sex, dob, major, intake_year, updated_at AS last_updated"
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def get_connection(conn_info: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(conn_info or ":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.executescript(_TRIGGER)
    conn.commit()


def reset_database(conn_info: Optional[Path] = None) -> sqlite3.Connection:
    if conn_info and Path(conn_info).exists():
        Path(conn_info).unlink()
    conn = get_connection(conn_info)
    init_schema(conn)
    return conn


def insert_student(conn: sqlite3.Connection, student: ExampleUnivStudent) -> None:
    conn.execute(
        """INSERT INTO students
           (student_ref, given_name, family_name, contact_email, sex, dob,
            major, intake_year, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            student.student_ref, student.given_name, student.family_name,
            student.contact_email, student.sex, student.dob.isoformat(),
            student.major, student.intake_year, _iso(student.updated_at),
        ),
    )
    conn.commit()


def update_fields(conn: sqlite3.Connection, table: str, source_id: str, fields: dict) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    params = list(fields.values()) + [source_id]
    conn.execute(f"UPDATE {table} SET {set_clause} WHERE student_ref = ?", params)
    conn.commit()


def delete_row(conn: sqlite3.Connection, table: str, source_id: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE student_ref = ?", (source_id,))
    conn.commit()


def fetch_changed(conn: sqlite3.Connection, table: str, watermark: Optional[datetime]) -> list[dict]:
    watermark_str = "0000-01-01T00:00:00" if watermark is None else _iso(watermark)
    cur = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM {table} WHERE updated_at > ? ORDER BY updated_at ASC",
        (watermark_str,),
    )
    return [dict(row) for row in cur.fetchall()]


def fetch_page(conn: sqlite3.Connection, table: str, limit: int, offset: int) -> list[dict]:
    cur = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM {table} ORDER BY student_ref ASC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(row) for row in cur.fetchall()]


def fetch_ids(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"SELECT student_ref FROM {table}")
    return [row["student_ref"] for row in cur.fetchall()]


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def row_by_id(conn: sqlite3.Connection, table: str, source_id: str) -> Optional[dict]:
    cur = conn.execute(f"SELECT {_SELECT_COLUMNS} FROM {table} WHERE student_ref = ?", (source_id,))
    row = cur.fetchone()
    return dict(row) if row else None
