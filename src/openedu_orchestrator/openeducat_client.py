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

import http.client
import socket
import sqlite3
import time
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from openedu_orchestrator.config import (
    ODOO_DB,
    ODOO_PASSWORD,
    ODOO_URL,
    ODOO_USERNAME,
    RPC_RETRY_BASE_DELAY_SECONDS,
    RPC_RETRY_MAX_ATTEMPTS,
    OPENEDUCAT_DB_PATH,
)
from openedu_orchestrator.logging_config import get_logger
from openedu_orchestrator import real_target_reference_data as refdata

logger = get_logger(__name__)

# Transient, network-level failures only -- deliberately excludes
# xmlrpc.client.Fault, which is Odoo's own application-level error (a
# rejected write, a validation failure); retrying that would just repeat
# the same rejection rather than recover from anything.
_TRANSIENT_RPC_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    OSError,
    socket.error,
    http.client.HTTPException,
    xmlrpc.client.ProtocolError,
)


def _call_with_retry(
    fn: Callable[[], Any],
    max_attempts: int = RPC_RETRY_MAX_ATTEMPTS,
    base_delay: float = RPC_RETRY_BASE_DELAY_SECONDS,
) -> Any:
    """Exponential backoff (base_delay * 2**attempt) around a single transient
    RPC call. Attempt count and delay come from config so they can be tuned
    without touching this logic.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except _TRANSIENT_RPC_EXCEPTIONS as exc:
            attempt += 1
            if attempt >= max_attempts:
                logger.error(
                    "rpc_retry_exhausted",
                    extra={"attempt": attempt, "max_attempts": max_attempts, "exception": str(exc)},
                )
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "rpc_retry",
                extra={"attempt": attempt, "max_attempts": max_attempts, "delay_seconds": delay, "exception": str(exc)},
            )
            time.sleep(delay)


_MODEL_TABLE = {
    "op.student": "op_student",
    "op.faculty": "op_faculty",
    "op.course": "op_course",
    "op.exam.attendees": "op_exam_attendees",
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

-- Flattened stand-in for real op.exam.attendees. The real model reaches its
-- student and exam through many2one ids resolved from external IDs; the mock
-- keeps the source references as plain text, because reproducing Odoo's
-- relational graph here would make the test double harder to reason about
-- without testing anything the real target does not already cover.
CREATE TABLE IF NOT EXISTS op_exam_attendees (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pieas_id      TEXT UNIQUE,
    student_ref   TEXT,
    subject_ref   TEXT,
    exam_name     TEXT,
    marks         INTEGER,
    total_marks   INTEGER,
    status        TEXT,
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

    def supports_active(self, model: str) -> bool:
        """Every mock table carries an `active` column, so this is always
        True here. Present for interface parity with OdooXmlRpcClient, where
        it genuinely varies by model.
        """
        self._table(model)  # keep the unknown-model error behaviour consistent
        return True

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

    def search_count(self, model: str, domain: Optional[list[tuple]] = None) -> int:
        table = self._table(model)
        where_sql, params = self._domain_to_sql(domain or [])
        query = f"SELECT COUNT(*) AS n FROM {table}"
        if where_sql:
            query += f" WHERE {where_sql}"
        return self._conn.execute(query, params).fetchone()["n"]

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


class OdooXmlRpcClient:
    """Real OpenEduCat/Odoo access over XML-RPC.

    Exposes the exact same method surface as the mock `OpenEduCatClient`
    (`create`, `write`, `archive`, `search_read`, `read`) so `LoaderAgent`
    and `ValidationAgent` need zero changes to use this instead -- only the
    class constructed at the call site changes. Model names (`op.student`,
    `op.faculty`, `op.course`) map directly to real Odoo model names, so no
    table-translation layer is needed here the way the SQLite mock needs one.
    """

    def __init__(
        self,
        url: str = ODOO_URL,
        db: str = ODOO_DB,
        username: str = ODOO_USERNAME,
        password: str = ODOO_PASSWORD,
    ):
        self.url = url
        self.db = db
        self.password = password
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self.uid = common.authenticate(db, username, password, {})
        if not self.uid:
            raise ConnectionError(
                f"Odoo authentication failed for user {username!r} against db {db!r} at {url}"
            )
        self._models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        self._supports_active_cache: dict[str, bool] = {}

    def _execute(
        self, model: str, method: str, args: Optional[list] = None, kwargs: Optional[dict] = None
    ) -> Any:
        """`execute_kw`'s real signature takes positional `args` and keyword
        `kwargs` as two separate parameters -- they must not be merged into
        one list, or Odoo's ORM receives the kwargs dict as a misplaced
        positional argument (e.g. as `fields` on search_read).

        Wrapped in retry/backoff for transient network failures (see
        _call_with_retry) -- covers every RPC this client makes (create,
        write, search_read, read, archive all funnel through here), not
        just writes, since a dropped connection can hit a read just as
        easily as a write.
        """
        return _call_with_retry(lambda: self._models.execute_kw(
            self.db, self.uid, self.password, model, method, args or [], kwargs or {}
        ))

    def close(self) -> None:
        """No persistent connection to close for XML-RPC; kept for interface parity."""

    _XMLID_MODULE = "openedu_sync"

    def _xmlid_name(self, model: str, external_id: str) -> str:
        return f"{model.replace('.', '_')}_{external_id}"

    def _set_external_id(self, model: str, res_id: int, external_id: str) -> None:
        """Register a source_id -> Odoo record link via Odoo's own external-ID
        mechanism (ir.model.data) -- real OpenEduCat models have no field to
        hold a foreign-system key, so this is the standard Odoo-native way to
        track it, discoverable from the Odoo side without depending on our
        own sync_mapping store. Works identically regardless of which
        source system the id came from.
        """
        self._execute("ir.model.data", "create", [{
            "module": self._XMLID_MODULE,
            "name": self._xmlid_name(model, external_id),
            "model": model,
            "res_id": res_id,
        }])

    def find_by_external_id(self, model: str, external_id: str) -> Optional[int]:
        rows = self._execute(
            "ir.model.data", "search_read",
            [[("module", "=", self._XMLID_MODULE), ("name", "=", self._xmlid_name(model, external_id))]],
            {"fields": ["res_id"]},
        )
        return rows[0]["res_id"] if rows else None

    @staticmethod
    def _marshal_values(values: dict[str, Any]) -> dict[str, Any]:
        """Convert Python None to Odoo's False before it reaches XML-RPC.

        Two separate reasons, and both matter:

        1. xmlrpc.client literally cannot serialise None unless the proxy is
           built with allow_none=True -- it raises
           "TypeError: cannot marshal None unless allow_none is enabled"
           before the request is even sent. So a source field that has been
           cleared to NULL would fail the whole write rather than clear the
           target field.
        2. False is Odoo's own canonical "empty" across field types: it
           blanks a Char/Text, unlinks a Many2one, and zeroes an Integer.
           Sending False is therefore the correct way to express "this field
           now has no value", which is exactly what a cleared source field
           means under source-is-authoritative semantics.

        Note this deliberately does NOT use allow_none=True on the proxy.
        That would send XML-RPC's <nil/> extension, which Odoo maps to a
        Python None the ORM treats inconsistently across field types --
        False is what Odoo itself uses internally, so it is what we send.
        """
        return {k: (False if v is None else v) for k, v in values.items()}

    def create(self, model: str, values: dict[str, Any]) -> int:
        values = dict(values)  # don't mutate the caller's dict
        source_id = values.pop("source_id", None)
        values, refs = refdata.split_references(values)
        field_updates, post_write = refdata.resolve(self, model, refs)
        values.update(field_updates)
        new_id = self._execute(model, "create", [self._marshal_values(values)])
        if source_id:
            self._set_external_id(model, new_id, source_id)
        if post_write is not None:
            # Links that can only be made once the record exists (e.g. a
            # student's enrollment record). Deliberately after the external
            # ID is registered, so a failure here still leaves the record
            # discoverable and the next run can repair it rather than
            # creating a duplicate.
            post_write(new_id)
        return new_id

    def write(self, model: str, record_id: int, values: dict[str, Any]) -> bool:
        """Same source_id handling as create(): the external ID was already
        registered at create time, so on update it's just discarded rather
        than re-registered. Found by a real update failing with 'Invalid
        field pieas_id' (before this was renamed to source_id) -- create()
        had this special-case, write() didn't.
        """
        if not values:
            return True
        values = dict(values)
        values.pop("source_id", None)
        values, refs = refdata.split_references(values)
        field_updates, post_write = refdata.resolve(self, model, refs)
        values.update(field_updates)
        ok = self._execute(model, "write", [[record_id], self._marshal_values(values)]) if values else True
        if post_write is not None:
            # Idempotent by construction (see enroll_student), so re-running
            # an update is safe. A record whose department genuinely changed
            # gains a second enrollment rather than losing the first --
            # correct for an academic history, and consistent with the
            # archive-don't-delete stance taken for deletions.
            post_write(record_id)
        return ok

    def supports_active(self, model: str) -> bool:
        """Whether a model has Odoo's `active` field -- i.e. whether it can
        be archived at all.

        Not every model has one: real op.exam.attendees does not, which is
        only discoverable by asking the instance. Cached, since a deletion
        cycle asks once per record but the answer cannot change mid-run.
        """
        if model not in self._supports_active_cache:
            fields = self._execute(model, "fields_get", [[]], {"attributes": ["type"]})
            self._supports_active_cache[model] = "active" in fields
        return self._supports_active_cache[model]

    def archive(self, model: str, record_id: int) -> bool:
        """write(model, id, {'active': False}) -- the report's adopted deletion handling.

        Models with no `active` field cannot be archived. This raises rather
        than silently doing nothing or hard-deleting instead -- the report
        rejects hard delete as irreversible (Section 5.4), so the right
        outcome is a visible error in the run report and an explicit human
        decision for that model, not a quiet fallback.
        """
        if not self.supports_active(model):
            raise ValueError(
                f"{model} has no 'active' field, so it cannot be archived. Hard-deleting "
                f"instead is rejected by the report's Section 5.4 as irreversible, so this "
                f"model needs an explicit deletion policy rather than a silent fallback."
            )
        return self.write(model, record_id, {"active": False})

    def search_read(
        self,
        model: str,
        domain: Optional[list[tuple]] = None,
        fields: Optional[list[str]] = None,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {"fields": fields} if fields else {}
        return self._execute(model, "search_read", [domain or []], kwargs)

    def search_count(self, model: str, domain: Optional[list[tuple]] = None) -> int:
        """Real Odoo's own search_count -- doesn't fetch/compute any field
        values at all, unlike search_read, so it sidesteps a real bug found
        by testing: search_read with no explicit `fields` list triggers
        computing *every* field, including a broken computed field in
        openeducat_fees that crashes on multi-record recordsets
        ("Expected singleton"). Counting should never need to compute
        fields in the first place.
        """
        return self._execute(model, "search_count", [domain or []])

    def read(self, model: str, ids: list[int], fields: Optional[list[str]] = None) -> list[dict]:
        if not ids:
            return []
        kwargs: dict[str, Any] = {"fields": fields} if fields else {}
        return self._execute(model, "read", [ids], kwargs)
