"""Orchestrator's private state store: `sync_mapping` + `sync_state`.

Per the report (Section 3.1, Section 6.1), this table is owned and read/written
*exclusively* by the Orchestrator Agent. No other agent -- Extractor,
Transformer, or Loader -- should import this module. That is not just a
convention here: none of the other agent modules are given a path to this
database file.

Scoped by source_system (not just entity_type) so two different source
systems can sync the same entity_type against the same target without
colliding -- each has its own watermark and its own mapping rows, even if
a source_id happened to collide between them. Before this, sync_mapping
was implicitly PIEAS-only: the unique constraint was (pieas_id,
entity_type), so a second source system syncing "student" would have
fought over the same rows and the same watermark as PIEAS's.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from openedu_orchestrator.config import SYNC_STORE_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_mapping (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system   TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    openeducat_id   INTEGER NOT NULL,
    entity_type     TEXT NOT NULL,
    content_hash    TEXT,
    archived        INTEGER NOT NULL DEFAULT 0,
    last_synced_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000Z','now')),
    UNIQUE (source_system, source_id, entity_type)
);

CREATE TABLE IF NOT EXISTS sync_state (
    source_system   TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    last_synced_at  TEXT,
    target          TEXT,
    PRIMARY KEY (source_system, entity_type)
);
"""


def get_connection(db_path: Path = SYNC_STORE_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # CREATE TABLE IF NOT EXISTS will not add a column to a store that
    # already exists, so bring older files forward explicitly. Left NULL for
    # pre-existing rows, which get_target treats as "not yet known" and
    # adopts on the next run rather than guessing.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sync_state)")}
    if "target" not in existing:
        conn.execute("ALTER TABLE sync_state ADD COLUMN target TEXT")
    mapping_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sync_mapping)")}
    if "archived" not in mapping_cols:
        conn.execute("ALTER TABLE sync_mapping ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    conn.commit()


class TargetMismatchError(RuntimeError):
    """Raised when a cycle is about to run against a different target than
    the one that wrote the current mappings.

    sync_mapping stores openeducat_id values that are only meaningful in one
    target's id space. Running against a different target would hand those
    ids to a system where they identify completely unrelated records -- so
    an "update" silently overwrites the wrong row. Found by running the mock
    demo (which resets the store) on a machine that had also synced to a
    real Odoo: the store then mapped PIEAS-STU-00001 to id 1, which in the
    real instance was an unrelated OpenEduCat demo student.
    """


def get_target(conn: sqlite3.Connection, source_system: str, entity_type: str) -> Optional[str]:
    cur = conn.execute(
        "SELECT target FROM sync_state WHERE source_system = ? AND entity_type = ?",
        (source_system, entity_type),
    )
    row = cur.fetchone()
    return row["target"] if row else None


def set_target(conn: sqlite3.Connection, source_system: str, entity_type: str, target: str) -> None:
    conn.execute(
        """INSERT INTO sync_state (source_system, entity_type, target)
           VALUES (?, ?, ?)
           ON CONFLICT (source_system, entity_type) DO UPDATE SET target = excluded.target""",
        (source_system, entity_type, target),
    )
    conn.commit()


def reset_database(db_path: Path = SYNC_STORE_DB_PATH) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    conn = get_connection(db_path)
    init_schema(conn)
    return conn


def content_hash(fields: dict) -> str:
    """Stable hash of a record's business fields (excludes last_updated),
    so a re-run with no real change can be recognised as a no-op even if a
    timestamp got touched without a value actually changing.
    """
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- mapping lookups (Listing 5) --------------------------------------------

def get_mapping(
    conn: sqlite3.Connection, source_system: str, source_id: str, entity_type: str
) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM sync_mapping WHERE source_system = ? AND source_id = ? AND entity_type = ?",
        (source_system, source_id, entity_type),
    )
    return cur.fetchone()


def get_all_mappings(conn: sqlite3.Connection, source_system: str, entity_type: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT source_id, openeducat_id, content_hash, archived FROM sync_mapping "
        "WHERE source_system = ? AND entity_type = ?",
        (source_system, entity_type),
    )
    return cur.fetchall()


def count_mappings(conn: sqlite3.Connection, source_system: str, entity_type: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_mapping WHERE source_system = ? AND entity_type = ?",
        (source_system, entity_type),
    )
    return cur.fetchone()["n"]


# --- mapping writes (Listing 6) ----------------------------------------------

def upsert_mapping(
    conn: sqlite3.Connection,
    source_system: str,
    source_id: str,
    openeducat_id: int,
    entity_type: str,
    hash_: str,
) -> None:
    conn.execute(
        """INSERT INTO sync_mapping
               (source_system, source_id, openeducat_id, entity_type, content_hash, last_synced_at)
           VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%f000Z','now'))
           ON CONFLICT (source_system, source_id, entity_type)
           DO UPDATE SET openeducat_id = excluded.openeducat_id,
                         content_hash = excluded.content_hash,
                         archived = 0,
                         last_synced_at = excluded.last_synced_at""",
        (source_system, source_id, openeducat_id, entity_type, hash_),
    )
    conn.commit()


def mark_archived(conn: sqlite3.Connection, source_system: str, source_id: str, entity_type: str) -> None:
    """Record that this mapping's target record is currently archived.

    Kept locally rather than read back from the target, because the
    Orchestrator is the only agent allowed to hold sync state and cannot
    query the target -- and because checking would otherwise cost one RPC
    per record on every change cycle. Cleared by upsert_mapping, since a
    successful write means the record is live again.
    """
    conn.execute(
        """UPDATE sync_mapping SET archived = 1
           WHERE source_system = ? AND source_id = ? AND entity_type = ?""",
        (source_system, source_id, entity_type),
    )
    conn.commit()


def touch_mapping(conn: sqlite3.Connection, source_system: str, source_id: str, entity_type: str) -> None:
    """Bump last_synced_at on a mapping row without changing its target id
    (used after an archive: the mapping still points at the same OpenEduCat
    record, which now simply has active=False).
    """
    conn.execute(
        """UPDATE sync_mapping
           SET last_synced_at = strftime('%Y-%m-%dT%H:%M:%f000Z','now')
           WHERE source_system = ? AND source_id = ? AND entity_type = ?""",
        (source_system, source_id, entity_type),
    )
    conn.commit()


# --- watermark (Listing 7, generalised to a table per source_system/entity_type) ----------

def get_watermark(conn: sqlite3.Connection, source_system: str, entity_type: str) -> Optional[datetime]:
    cur = conn.execute(
        "SELECT last_synced_at FROM sync_state WHERE source_system = ? AND entity_type = ?",
        (source_system, entity_type),
    )
    row = cur.fetchone()
    if row is None or row["last_synced_at"] is None:
        return None
    return datetime.fromisoformat(row["last_synced_at"])


def advance_watermark(
    conn: sqlite3.Connection, source_system: str, entity_type: str, new_watermark: datetime
) -> None:
    conn.execute(
        """INSERT INTO sync_state (source_system, entity_type, last_synced_at) VALUES (?, ?, ?)
           ON CONFLICT (source_system, entity_type) DO UPDATE SET last_synced_at = excluded.last_synced_at""",
        (source_system, entity_type, new_watermark.isoformat()),
    )
    conn.commit()


def is_bulk_mode(conn: sqlite3.Connection, source_system: str, entity_type: str) -> bool:
    """BULK mode triggers when the state store has never seen this
    source_system/entity_type pair before -- Section 4: 'Orchestrator sees
    an empty state store.'
    """
    return (
        count_mappings(conn, source_system, entity_type) == 0
        and get_watermark(conn, source_system, entity_type) is None
    )
