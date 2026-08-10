"""Real MySQL-backed PIEAS source -- a drop-in counterpart to pieas_source.py
(the SQLite mock the test suite still runs against), exposing the identical
function surface (get_connection, fetch_changed, fetch_page, fetch_ids,
insert_*, update_fields, delete_row, count_rows, row_by_id, all_pieas_ids)
so ExtractorAgent can be pointed at either one interchangeably.

Deliberately a different database technology than OpenEduCat's Postgres
backend, not just a different database on the same server: a real legacy
campus system and a modern Odoo ERP would almost never share database
technology, and using the same one for both would understate exactly the
cross-system heterogeneity this project exists to prove out.

One genuine simplification over the SQLite version: MySQL's native
`ON UPDATE CURRENT_TIMESTAMP(6)` column attribute replaces the SQLite
version's hand-written trigger -- it already bumps last_updated on any
UPDATE that doesn't itself set that column explicitly, which is exactly
the semantic the SQLite trigger was built to replicate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

import pymysql
import pymysql.cursors

from openedu_orchestrator.models import PieasCourse, PieasFaculty, PieasStudent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    pieas_id      VARCHAR(32) PRIMARY KEY,
    roll_number   VARCHAR(64) NOT NULL,
    first_name    VARCHAR(128) NOT NULL,
    last_name     VARCHAR(128) NOT NULL,
    email         VARCHAR(255) NOT NULL,
    gender        VARCHAR(16) NOT NULL,
    date_of_birth DATE NOT NULL,
    department    VARCHAR(128) NOT NULL,
    batch_year    INT NOT NULL,
    last_updated  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_students_last_updated (last_updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS faculty (
    pieas_id      VARCHAR(32) PRIMARY KEY,
    employee_code VARCHAR(64) NOT NULL,
    first_name    VARCHAR(128) NOT NULL,
    last_name     VARCHAR(128) NOT NULL,
    email         VARCHAR(255) NOT NULL,
    gender        VARCHAR(16) NOT NULL,
    date_of_birth DATE NOT NULL,
    department    VARCHAR(128) NOT NULL,
    designation   VARCHAR(64) NOT NULL,
    last_updated  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_faculty_last_updated (last_updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS courses (
    pieas_id      VARCHAR(32) PRIMARY KEY,
    code          VARCHAR(32) NOT NULL,
    name          VARCHAR(255) NOT NULL,
    department    VARCHAR(128) NOT NULL,
    credit_hours  INT NOT NULL,
    semester      VARCHAR(16) NOT NULL,
    last_updated  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_courses_last_updated (last_updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DEFAULT_CONNECTION = dict(
    host="127.0.0.1", port=3307, user="pieas_app", password="pieas_app_pw", database="pieas_real",
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _normalize_row(row: dict) -> dict:
    """pymysql returns real datetime/date objects for DATETIME/DATE columns,
    not ISO strings the way sqlite3.Row does. Normalize here so this
    module's row shape is a true drop-in match for pieas_source.py's --
    downstream code (content_hash, watermark comparisons via
    datetime.fromisoformat) expects string values, same as the SQLite path.
    """
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def get_connection(conn_info: Optional[dict] = None) -> pymysql.connections.Connection:
    info = dict(DEFAULT_CONNECTION)
    if conn_info:
        info.update(conn_info)
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, autocommit=False, **info)


def init_schema(conn: pymysql.connections.Connection) -> None:
    with conn.cursor() as cur:
        for statement in _SCHEMA.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
    conn.commit()


def reset_database(conn_info: Optional[dict] = None) -> pymysql.connections.Connection:
    """Drop and recreate all three tables from scratch."""
    conn = get_connection(conn_info)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS students")
        cur.execute("DROP TABLE IF EXISTS faculty")
        cur.execute("DROP TABLE IF EXISTS courses")
    conn.commit()
    init_schema(conn)
    return conn


# --- seeding / mutation helpers (simulate "PIEAS is still being used") -----

def insert_student(conn: pymysql.connections.Connection, student: PieasStudent) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO students
               (pieas_id, roll_number, first_name, last_name, email, gender,
                date_of_birth, department, batch_year, last_updated)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                student.pieas_id, student.roll_number, student.first_name,
                student.last_name, student.email, student.gender,
                student.date_of_birth.isoformat(), student.department,
                student.batch_year, _iso(student.last_updated),
            ),
        )
    conn.commit()


def insert_faculty(conn: pymysql.connections.Connection, faculty: PieasFaculty) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO faculty
               (pieas_id, employee_code, first_name, last_name, email, gender,
                date_of_birth, department, designation, last_updated)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                faculty.pieas_id, faculty.employee_code, faculty.first_name,
                faculty.last_name, faculty.email, faculty.gender,
                faculty.date_of_birth.isoformat(), faculty.department,
                faculty.designation, _iso(faculty.last_updated),
            ),
        )
    conn.commit()


def insert_course(conn: pymysql.connections.Connection, course: PieasCourse) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO courses
               (pieas_id, code, name, department, credit_hours, semester, last_updated)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                course.pieas_id, course.code, course.name, course.department,
                course.credit_hours, course.semester, _iso(course.last_updated),
            ),
        )
    conn.commit()


def update_fields(conn: pymysql.connections.Connection, table: str, pieas_id: str, fields: dict) -> None:
    """Simulate an edit made on the PIEAS website. last_updated bumps via
    MySQL's native ON UPDATE CURRENT_TIMESTAMP(6) -- no explicit trigger
    needed, unlike the SQLite version.
    """
    if not fields:
        return
    set_clause = ", ".join(f"{col} = %s" for col in fields)
    params = list(fields.values()) + [pieas_id]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {set_clause} WHERE pieas_id = %s", params)
    conn.commit()


def delete_row(conn: pymysql.connections.Connection, table: str, pieas_id: str) -> None:
    """Simulate a record being removed from PIEAS -- a real DELETE, no soft-delete."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE pieas_id = %s", (pieas_id,))
    conn.commit()


# --- read paths the Extractor Agent is allowed to call ---------------------

def fetch_changed(conn: pymysql.connections.Connection, table: str, watermark: Optional[datetime]) -> list[dict]:
    """WHERE last_updated > :watermark ORDER BY last_updated ASC (Listing 1)."""
    watermark_str = "0000-01-01 00:00:00" if watermark is None else _iso(watermark)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {table} WHERE last_updated > %s ORDER BY last_updated ASC",
            (watermark_str,),
        )
        return [_normalize_row(row) for row in cur.fetchall()]


def fetch_page(conn: pymysql.connections.Connection, table: str, limit: int, offset: int) -> list[dict]:
    """Full-table pull, one page at a time, ordered for stable pagination."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY pieas_id ASC LIMIT %s OFFSET %s", (limit, offset))
        return [_normalize_row(row) for row in cur.fetchall()]


def fetch_ids(conn: pymysql.connections.Connection, table: str) -> list[str]:
    """SELECT pieas_id FROM <table> (Listing 2) -- IDs only, for the deletion cycle."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT pieas_id FROM {table}")
        return [row["pieas_id"] for row in cur.fetchall()]


def count_rows(conn: pymysql.connections.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return cur.fetchone()["n"]


def row_by_id(conn: pymysql.connections.Connection, table: str, pieas_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE pieas_id = %s", (pieas_id,))
        row = cur.fetchone()
        return _normalize_row(row) if row else None


def all_pieas_ids(conn: pymysql.connections.Connection, tables: Iterable[str]) -> dict[str, list[str]]:
    return {table: fetch_ids(conn, table) for table in tables}
