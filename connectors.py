"""
The two edges of the pipeline.

PieasDB   read-only access to the legacy MySQL. Nothing here writes, ever.
OdooRPC   writes to OpenEduCat over XML-RPC.

Why XML-RPC and not direct PostgreSQL: writing straight to Odoo's tables skips
the ORM, which means skipping computed fields, validation, and related-record
bookkeeping -- you get half-formed records that fail silently later. XML-RPC is
built into every self-hosted Odoo and enforces all business rules.
"""
import xmlrpc.client
from datetime import date, datetime
from decimal import Decimal

import mysql.connector

import config


# ══════════════════════════════════════════════════════════════════ PIEAS ══

class PieasDB:
    """Read-only handle on the legacy source."""

    def __init__(self):
        self.cfg = config.MYSQL

    def _connect(self, with_db=True):
        cfg = dict(self.cfg)
        if not with_db:
            cfg.pop("database", None)
        return mysql.connector.connect(**cfg)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as cn:
            cur = cn.cursor(dictionary=True)
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
        return [self._normalize(r) for r in rows]

    @staticmethod
    def _normalize(row: dict) -> dict:
        """Make values JSON- and XML-RPC-safe."""
        out = {}
        for k, v in row.items():
            if isinstance(v, Decimal):
                out[k] = float(v)
            elif isinstance(v, datetime):
                out[k] = v.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(v, date):
                out[k] = v.strftime("%Y-%m-%d")
            elif isinstance(v, (bytes, bytearray)):
                out[k] = v.decode(errors="replace")
            else:
                out[k] = v
        return out

    def schema(self, table: str) -> dict:
        """Columns + foreign keys, straight from information_schema.

        The FKs are what let the Transformer resolve a PIEAS `course_id` into the
        right Odoo id -- discovered, not hardcoded.
        """
        db = self.cfg["database"]
        # Aliases are mandatory: MySQL 8 returns information_schema column names
        # upper-cased, so `column_name` would come back as `COLUMN_NAME`.
        cols = self.query("""
            SELECT column_name  AS column_name,
                   data_type    AS data_type,
                   is_nullable  AS is_nullable,
                   column_type  AS column_type,
                   column_key   AS column_key
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position""", (db, table))
        fks = self.query("""
            SELECT column_name            AS column_name,
                   referenced_table_name  AS referenced_table_name
            FROM information_schema.key_column_usage
            WHERE table_schema=%s AND table_name=%s
              AND referenced_table_name IS NOT NULL""", (db, table))
        return {
            "table":   table,
            "columns": cols,
            "foreign_keys": {f["column_name"]: f["referenced_table_name"] for f in fks},
        }

    def run_script(self, path: str):
        """Used only by `run.py seed`."""
        with open(path, "r", encoding="utf-8") as fh:
            script = fh.read()
        cn = self._connect(with_db=False)
        cur = cn.cursor()
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        cn.commit()
        cur.close()
        cn.close()


# ═══════════════════════════════════════════════════════════════════ ODOO ══

class OdooRPC:
    """XML-RPC client for OpenEduCat, with a fields_get cache."""

    def __init__(self):
        c = config.ODOO
        self.url, self.db = c["url"], c["db"]
        self.common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        self.uid = self.common.authenticate(self.db, c["username"], c["password"], {})
        if not self.uid:
            raise RuntimeError(
                f"Odoo authentication failed for user '{c['username']}' on db '{self.db}'. "
                f"Check ODOO_* values in .env.")
        self.password = c["password"]
        self._fields: dict[str, dict] = {}

    def execute(self, model: str, method: str, *args, **kw):
        return self.models.execute_kw(self.db, self.uid, self.password,
                                      model, method, list(args), kw)

    def fields_of(self, model: str) -> dict:
        """Cached fields_get. This is what the Transformer reasons against."""
        if model not in self._fields:
            self._fields[model] = self.execute(
                model, "fields_get", [],
                attributes=["string", "type", "required", "readonly",
                            "relation", "selection"])
        return self._fields[model]

    def writable_fields(self, model: str) -> dict:
        """Fields we are actually allowed to set.

        Drops readonly fields -- notably related+stored ones such as
        op.exam.course_id, which is derived from session_id. Writing those
        raises, so no mapping plan is ever allowed to include them.
        """
        return {k: v for k, v in self.fields_of(model).items()
                if not v.get("readonly") and k not in ("id", "__last_update")}

    def create(self, model: str, vals: dict) -> int:
        return self.execute(model, "create", vals)

    def write(self, model: str, odoo_id: int, vals: dict) -> bool:
        return self.execute(model, "write", [odoo_id], vals)

    def can_archive(self, model: str) -> bool:
        return "active" in self.fields_of(model)

    def archive(self, model: str, odoo_id: int) -> str:
        """Retire a record that vanished from PIEAS.

        Archiving is always preferred: it keeps grades and enrolment history, and
        survives a PIEAS-side rename that only looked like a deletion.

        Not every model can be archived, though. Line models such as
        op.exam.attendees have no `active` field at all, so there is no archived
        state to move them into -- removal is the only way to stop reporting a
        result for an exam sitting that no longer exists. Returns which happened.
        """
        if self.can_archive(model):
            self.execute(model, "write", [odoo_id], {"active": False})
            return "archived"
        self.execute(model, "unlink", [odoo_id])
        return "deleted"

    def exists(self, model: str, odoo_id: int) -> bool:
        return bool(self.execute(model, "search_count",
                                 [["id", "=", odoo_id]], context={"active_test": False}))

    def installed(self, module: str) -> bool:
        return bool(self.execute("ir.module.module", "search_count",
                                 [["name", "=", module], ["state", "=", "installed"]]))
