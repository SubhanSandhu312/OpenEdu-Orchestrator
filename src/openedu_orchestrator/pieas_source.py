"""Dummy PIEAS website database.

This stands in for the real PIEAS system. It is deliberately dumb: plain
tables, a `last_updated` column that a trigger bumps on any UPDATE that
doesn't set it explicitly, and no soft-delete concept (a row that "disappears
from PIEAS" is genuinely DELETEd here, exactly as the report assumes -- PIEAS
exposes no API/webhooks and no notion of an undo).

Only the Extractor Agent should import and call into this module. Everything
else in the pipeline receives PIEAS data secondhand, through the Extractor.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from openedu_orchestrator.config import PIEAS_DB_PATH, PIEAS_TABLE_FOR_ENTITY
from openedu_orchestrator.models import PieasCourse, PieasFaculty, PieasStudent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    pieas_id      TEXT PRIMARY KEY,
    roll_number   TEXT NOT NULL,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    email         TEXT NOT NULL,
    gender        TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    department    TEXT NOT NULL,
    batch_year    INTEGER NOT NULL,
    last_updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faculty (
    pieas_id      TEXT PRIMARY KEY,
    employee_code TEXT NOT NULL,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    email         TEXT NOT NULL,
    gender        TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    department    TEXT NOT NULL,
    designation   TEXT NOT NULL,
    last_updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    pieas_id      TEXT PRIMARY KEY,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    department    TEXT NOT NULL,
    credit_hours  INTEGER NOT NULL,
    semester      TEXT NOT NULL,
    last_updated  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_students_last_updated ON students(last_updated);
CREATE INDEX IF NOT EXISTS idx_faculty_last_updated ON faculty(last_updated);
CREATE INDEX IF NOT EXISTS idx_courses_last_updated ON courses(last_updated);
"""

# One trigger per table: if an UPDATE statement did not itself set
# last_updated, bump it to now. This is what lets `update_fields()` below
# simulate "PIEAS changed this record" without every caller having to
# remember to touch the timestamp -- exactly how most real databases behave.
_TRIGGER_TEMPLATE = """
CREATE TRIGGER IF NOT EXISTS trg_{table}_touch
AFTER UPDATE ON {table}
FOR EACH ROW
WHEN NEW.last_updated = OLD.last_updated
BEGIN
    UPDATE {table}
    SET last_updated = strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
    WHERE pieas_id = NEW.pieas_id;
END;
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def get_connection(db_path: Path = PIEAS_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for table in PIEAS_TABLE_FOR_ENTITY.values():
        conn.executescript(_TRIGGER_TEMPLATE.format(table=table))
    conn.commit()


def reset_database(db_path: Path = PIEAS_DB_PATH) -> sqlite3.Connection:
    """Delete and recreate the PIEAS dummy database from scratch."""
    if db_path.exists():
        db_path.unlink()
    conn = get_connection(db_path)
    init_schema(conn)
    return conn


# --- seeding / mutation helpers (simulate "PIEAS is still being used") -----

def insert_student(conn: sqlite3.Connection, student: PieasStudent) -> None:
    conn.execute(
        """INSERT INTO students
           (pieas_id, roll_number, first_name, last_name, email, gender,
            date_of_birth, department, batch_year, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            student.pieas_id, student.roll_number, student.first_name,
            student.last_name, student.email, student.gender,
            student.date_of_birth.isoformat(), student.department,
            student.batch_year, _iso(student.last_updated),
        ),
    )
    conn.commit()


def insert_faculty(conn: sqlite3.Connection, faculty: PieasFaculty) -> None:
    conn.execute(
        """INSERT INTO faculty
           (pieas_id, employee_code, first_name, last_name, email, gender,
            date_of_birth, department, designation, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            faculty.pieas_id, faculty.employee_code, faculty.first_name,
            faculty.last_name, faculty.email, faculty.gender,
            faculty.date_of_birth.isoformat(), faculty.department,
            faculty.designation, _iso(faculty.last_updated),
        ),
    )
    conn.commit()


def insert_course(conn: sqlite3.Connection, course: PieasCourse) -> None:
    conn.execute(
        """INSERT INTO courses
           (pieas_id, code, name, department, credit_hours, semester, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            course.pieas_id, course.code, course.name, course.department,
            course.credit_hours, course.semester, _iso(course.last_updated),
        ),
    )
    conn.commit()


def update_fields(conn: sqlite3.Connection, table: str, source_id: str, fields: dict) -> None:
    """Simulate an edit made on the PIEAS website. Bumps last_updated via trigger."""
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    params = list(fields.values()) + [source_id]
    conn.execute(f"UPDATE {table} SET {set_clause} WHERE pieas_id = ?", params)
    conn.commit()


def delete_row(conn: sqlite3.Connection, table: str, source_id: str) -> None:
    """Simulate a record being removed from PIEAS -- a real DELETE, no soft-delete."""
    conn.execute(f"DELETE FROM {table} WHERE pieas_id = ?", (source_id,))
    conn.commit()


# --- read paths the Extractor Agent is allowed to call ---------------------
#
# Every read function renames PIEAS's own primary key column (pieas_id --
# a perfectly natural name for PIEAS's own schema) to the pipeline's
# generic wire-format key, source_id, in its returned dict -- the same
# convention pieas_source_mysql.py and example_univ_source.py follow, so
# nothing downstream (Orchestrator, sync_store, graph.py) has a
# PIEAS-specific assumption left in it. PIEAS's last_updated column
# already matches the pipeline's generic watermark key name, so no rename
# is needed there.

def _normalize_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["source_id"] = d.pop("pieas_id")
    return d


def fetch_changed(conn: sqlite3.Connection, table: str, watermark: Optional[datetime]) -> list[dict]:
    """WHERE last_updated > :watermark ORDER BY last_updated ASC (Listing 1)."""
    if watermark is None:
        watermark_str = "0000-01-01T00:00:00"
    else:
        watermark_str = _iso(watermark)
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE last_updated > ? ORDER BY last_updated ASC",
        (watermark_str,),
    )
    return [_normalize_row(row) for row in cur.fetchall()]


def fetch_page(conn: sqlite3.Connection, table: str, limit: int, offset: int) -> list[dict]:
    """Full-table pull, one page at a time, ordered for stable pagination."""
    cur = conn.execute(
        f"SELECT * FROM {table} ORDER BY pieas_id ASC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [_normalize_row(row) for row in cur.fetchall()]


def fetch_ids(conn: sqlite3.Connection, table: str) -> list[str]:
    """SELECT pieas_id FROM <table> (Listing 2) -- IDs only, for the deletion cycle."""
    cur = conn.execute(f"SELECT pieas_id FROM {table}")
    return [row["pieas_id"] for row in cur.fetchall()]


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def row_by_id(conn: sqlite3.Connection, table: str, source_id: str) -> Optional[dict]:
    cur = conn.execute(f"SELECT * FROM {table} WHERE pieas_id = ?", (source_id,))
    row = cur.fetchone()
    return _normalize_row(row) if row else None


def all_pieas_ids(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, list[str]]:
    return {table: fetch_ids(conn, table) for table in tables}
