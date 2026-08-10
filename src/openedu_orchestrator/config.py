"""Central configuration: db locations and the entity types the system knows about.

Three separate SQLite files stand in for three separate real systems, and this
separation is deliberate, not incidental: it makes it structurally impossible for
the wrong agent to touch the wrong store, mirroring the ownership rules in the
report (Extractor <-> PIEAS only, Orchestrator <-> sync store only, Loader <->
OpenEduCat only).
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Source system: dummy PIEAS website database. Only the Extractor connects here.
PIEAS_DB_PATH = DATA_DIR / "pieas.db"

# Target system: dummy OpenEduCat (Odoo) database, accessed only through
# OpenEduCatClient's ORM-shaped methods (create/write/search_read), never raw SQL
# writes from outside the client -- this mirrors "writes must go through the ORM".
OPENEDUCAT_DB_PATH = DATA_DIR / "openeducat_mock.db"

# Orchestrator's own state store: sync_mapping + sync_state. Only the
# Orchestrator ever opens this file.
SYNC_STORE_DB_PATH = DATA_DIR / "orchestrator_state.db"

# Entity types the pipeline knows how to move. Each maps a PIEAS table to an
# OpenEduCat model.
ENTITY_TYPES = ("student", "faculty", "course")

PIEAS_TABLE_FOR_ENTITY = {
    "student": "students",
    "faculty": "faculty",
    "course": "courses",
}

OPENEDUCAT_MODEL_FOR_ENTITY = {
    "student": "op.student",
    "faculty": "op.faculty",
    "course": "op.course",
}

# Incremental sync scheduling (informational for the CLI's --loop mode; the
# report specifies change cycles every 15-60 min and deletion cycles ~daily).
CHANGE_CYCLE_INTERVAL_SECONDS = 30 * 60
DELETION_CYCLE_INTERVAL_SECONDS = 24 * 60 * 60

# Bulk migration pagination size (demonstrates "paginate until exhausted").
BULK_PAGE_SIZE = 25

# Real OpenEduCat/Odoo instance (local dev build: Odoo 19.0 + OpenEduCat 19.0
# from source, running on port 8070 -- see odoo-openeducat/ sibling project
# dir). Used by OdooXmlRpcClient, the real counterpart to the SQLite-backed
# OpenEduCatClient mock the test suite still runs against.
ODOO_URL = "http://localhost:8070"
ODOO_DB = "openeducat_test"
ODOO_USERNAME = "admin"
ODOO_PASSWORD = "admin"

# Retry/backoff for OdooXmlRpcClient's RPC calls -- only transient,
# network-level failures are retried (connection reset, timeout); Odoo
# application-level errors (xmlrpc.client.Fault, e.g. a validation failure)
# are never retried since retrying would just repeat the same rejection.
RPC_RETRY_MAX_ATTEMPTS = 3
RPC_RETRY_BASE_DELAY_SECONDS = 1.0
