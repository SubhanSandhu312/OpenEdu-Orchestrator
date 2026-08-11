"""
The state store -- owned exclusively by the Orchestrator.

Two things live here and nowhere else:

  sync_mapping    the PIEAS id <-> Odoo id bridge. Also doubles as the index the
                  deletion scan LEFT JOINs against to find ghosts.
  sync_watermark  the strictly advancing `last_updated` high-water mark per
                  entity. This is what makes the fast pulse correct: we never
                  ask PIEAS "what is dirty?", we ask "what changed after T?".

It is a local SQLite file on purpose. PIEAS stays read-only (zero modifications
to the legacy source) and Odoo stays clean of sync bookkeeping.
"""
import sqlite3
import hashlib
import json
from datetime import datetime

import config

_EPOCH = "1970-01-01 00:00:00"


def _conn():
    c = sqlite3.connect(config.STATE_DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sync_mapping (
            entity         TEXT NOT NULL,
            pieas_id       INTEGER NOT NULL,
            odoo_model     TEXT NOT NULL,
            odoo_id        INTEGER NOT NULL,
            row_hash       TEXT,
            status         TEXT NOT NULL DEFAULT 'active',   -- active | archived
            last_synced_at TEXT,
            PRIMARY KEY (entity, pieas_id)
        );
        CREATE TABLE IF NOT EXISTS sync_watermark (
            entity             TEXT PRIMARY KEY,
            watermark          TEXT NOT NULL,
            last_deletion_scan TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_run (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            phase     TEXT, entity TEXT,
            created   INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
            archived  INTEGER DEFAULT 0, failed  INTEGER DEFAULT 0,
            detail    TEXT, ran_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_map_entity ON sync_mapping(entity);
        """)


# ─────────────────────────────────────────────────────────────── watermarks ──

def get_watermark(entity: str) -> str:
    with _conn() as c:
        r = c.execute("SELECT watermark FROM sync_watermark WHERE entity=?",
                      (entity,)).fetchone()
        return r["watermark"] if r else _EPOCH


def advance_watermark(entity: str, new: str):
    """Only ever moves forward. Called after the Loader confirms success."""
    if not new:
        return
    cur = get_watermark(entity)
    if str(new) <= str(cur):
        return
    with _conn() as c:
        c.execute("""INSERT INTO sync_watermark(entity, watermark) VALUES(?,?)
                     ON CONFLICT(entity) DO UPDATE SET watermark=excluded.watermark""",
                  (entity, str(new)))


def record_deletion_scan(entity: str):
    with _conn() as c:
        c.execute("""INSERT INTO sync_watermark(entity, watermark, last_deletion_scan)
                     VALUES(?,?,?)
                     ON CONFLICT(entity) DO UPDATE SET last_deletion_scan=excluded.last_deletion_scan""",
                  (entity, _EPOCH, datetime.now().isoformat(timespec="seconds")))


# ────────────────────────────────────────────────────────────────── mapping ──

def row_hash(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def lookup(entity: str, pieas_id) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM sync_mapping WHERE entity=? AND pieas_id=?",
                      (entity, int(pieas_id))).fetchone()
        return dict(r) if r else None


def lookup_odoo_id(entity: str, pieas_id) -> int | None:
    r = lookup(entity, pieas_id)
    return r["odoo_id"] if r else None


def upsert(entity, pieas_id, odoo_model, odoo_id, rhash, status="active"):
    with _conn() as c:
        c.execute("""INSERT INTO sync_mapping
                       (entity, pieas_id, odoo_model, odoo_id, row_hash, status, last_synced_at)
                     VALUES (?,?,?,?,?,?,?)
                     ON CONFLICT(entity, pieas_id) DO UPDATE SET
                       odoo_id=excluded.odoo_id, row_hash=excluded.row_hash,
                       status=excluded.status, last_synced_at=excluded.last_synced_at""",
                  (entity, int(pieas_id), odoo_model, int(odoo_id), rhash, status,
                   datetime.now().isoformat(timespec="seconds")))


def mark_archived(entity: str, pieas_id):
    with _conn() as c:
        c.execute("""UPDATE sync_mapping SET status='archived', last_synced_at=?
                     WHERE entity=? AND pieas_id=?""",
                  (datetime.now().isoformat(timespec="seconds"), entity, int(pieas_id)))


def known_ids(entity: str) -> list[int]:
    """Every PIEAS id we believe is still live. The left side of the ghost hunt."""
    with _conn() as c:
        return [r["pieas_id"] for r in c.execute(
            "SELECT pieas_id FROM sync_mapping WHERE entity=? AND status='active'",
            (entity,))]


def count(entity: str) -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM sync_mapping WHERE entity=?",
                         (entity,)).fetchone()["n"]


def is_empty(entity: str) -> bool:
    """An empty state store is the trigger for Phase 1: bulk migration."""
    return count(entity) == 0


# ─────────────────────────────────────────────────────────────────── runlog ──

def log_run(phase, entity, created=0, updated=0, archived=0, failed=0, detail=""):
    with _conn() as c:
        c.execute("""INSERT INTO sync_run(phase, entity, created, updated, archived,
                                          failed, detail, ran_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (phase, entity, created, updated, archived, failed, detail[:2000],
                   datetime.now().isoformat(timespec="seconds")))


def status_report() -> list[dict]:
    with _conn() as c:
        out = []
        for table, model in config.ENTITIES:
            w = c.execute("SELECT * FROM sync_watermark WHERE entity=?", (table,)).fetchone()
            out.append({
                "entity":    table,
                "model":     model,
                "active":    c.execute("SELECT COUNT(*) n FROM sync_mapping WHERE entity=? AND status='active'", (table,)).fetchone()["n"],
                "archived":  c.execute("SELECT COUNT(*) n FROM sync_mapping WHERE entity=? AND status='archived'", (table,)).fetchone()["n"],
                "watermark": (w["watermark"] if w else _EPOCH),
                "last_deletion_scan": (w["last_deletion_scan"] if w else None),
            })
        return out
