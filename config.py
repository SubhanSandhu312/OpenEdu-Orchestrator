"""
PIEAS -> OpenEduCat synchronization: all settings.

Everything you are likely to change lives in the SCHEDULE block at the very top.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULE  --  edit these, nothing else needs to change
# ══════════════════════════════════════════════════════════════════════════════

# THE FAST PULSE -- catches inserts and updates via the last_updated watermark.
INCREMENTAL_EVERY_MINUTES = 15

# THE SLOW PULSE -- catches deletions by hunting for "ghosts" (rows that vanished
# from PIEAS but still exist in OpenEduCat). Cheaper than the fast pulse but
# still a full ID scan, so it runs far less often. 24h clock.
DELETION_SCAN_HOUR   = 2
DELETION_SCAN_MINUTE = 0

# Rows pushed to Odoo per batch.
BATCH_SIZE = 200

# WHAT GOES WHERE.  Order matters: parents before children, so that a child's
# foreign keys can be resolved to real Odoo IDs by the time it is synced.
# Add a row here and the whole pipeline picks it up -- no other code changes.
ENTITIES = [
    # (MySQL table,   OpenEduCat model)
    ("departments",   "op.department"),
    ("courses",       "op.course"),
    ("batches",       "op.batch"),
    ("subjects",      "op.subject"),
    ("faculty",       "op.faculty"),
    ("students",      "op.student"),
    ("exams",         "op.exam"),            # -> Examination module
    ("exam_results",  "op.exam.attendees"),  # -> Examination module
]

# ══════════════════════════════════════════════════════════════════════════════
#  Connections  (secrets come from .env)
# ══════════════════════════════════════════════════════════════════════════════

MYSQL = {
    "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER", "edu"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "pieas_lms"),
}

ODOO = {
    "url":      os.getenv("ODOO_URL", "http://localhost:8069"),
    "db":       os.getenv("ODOO_DB", "edu"),
    "username": os.getenv("ODOO_USER", "edu"),
    "password": os.getenv("ODOO_PASSWORD", ""),
}

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Flash-lite: the daily free-tier request quota is per-model, and the headline
# models are capped at 20/day, which a single cold bulk run would exhaust.
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# ══════════════════════════════════════════════════════════════════════════════
#  Paths
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
STATE_DB     = os.path.join(BASE_DIR, "sync_state.db")     # Orchestrator-owned
PLAN_CACHE   = os.path.join(BASE_DIR, "plans")             # cached LLM reasoning
SEED_SQL     = os.path.join(BASE_DIR, "sql", "pieas_seed.sql")

# The Extractor may only ever read from these tables. Anything else is rejected
# before it reaches MySQL, no matter what the model generates.
ALLOWED_TABLES = {t for t, _ in ENTITIES}
