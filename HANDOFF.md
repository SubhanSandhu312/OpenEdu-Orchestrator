# Handoff Documentation — Agentic PIEAS → OpenEduCat Synchronization

**Status:** Working proof of concept, validated end-to-end against live MySQL and live Odoo 19.
**Handed off:** August 2026. The original author is no longer maintaining this project.
**Repo:** <https://github.com/SubhanSandhu312/OpenEdu-Orchestrator>

You are now the owner. This document assumes you have the codebase and nothing else — no
prior conversations, no tribal knowledge. Everything you need to run, understand, debug,
and extend the system is here, including the things that will waste your day if nobody
warns you about them.

Read §1–§4 to get it running. Read §5–§8 to understand it. Read **§10 (Known Issues)
before you promise anything to anyone** — there is one significant functional gap.

---

## Table of contents

1. [What this project is](#1-what-this-project-is)
2. [Prerequisites](#2-prerequisites)
3. [Setting up from zero](#3-setting-up-from-zero)
4. [First run and verification](#4-first-run-and-verification)
5. [Architecture](#5-architecture)
6. [Code map](#6-code-map)
7. [The state store](#7-the-state-store)
8. [Where the LLM is used](#8-where-the-llm-is-used)
9. [Operations runbook](#9-operations-runbook)
10. [Known issues, bugs, and limitations](#10-known-issues-bugs-and-limitations)
11. [Gotchas that will waste your day](#11-gotchas-that-will-waste-your-day)
12. [How to extend it](#12-how-to-extend-it)
13. [Debugging playbook](#13-debugging-playbook)
14. [Production readiness gaps](#14-production-readiness-gaps)
15. [Suggested roadmap](#15-suggested-roadmap)
16. [Glossary](#16-glossary)

---

## 1. What this project is

PIEAS runs a legacy LMS on MySQL. It has **no API, no webhooks, and no way to announce
that something changed**. The institute is moving to OpenEduCat, an education ERP built on
Odoo 19. Something has to keep the ERP continuously in step with the legacy system.

Because the source cannot push, the target must **pull and hunt**. This project does that
with a four-agent [LangGraph](https://langchain-ai.github.io/langgraph/) pipeline:

| Agent | Job | Holds state? |
|---|---|---|
| **Orchestrator** | The only decision-maker. Owns the state store, picks each entity's phase, writes instructions, detects deletions, advances watermarks. | **Yes — exclusively** |
| **Extractor** | The only agent that touches MySQL. Turns an instruction into validated read-only SQL, returns rows. | No |
| **Transformer** | Pure function. MySQL row → Odoo-shaped values. | No |
| **Loader** | The only agent that writes. Create / update / archive over XML-RPC. | No |

Three phases share **one** pipeline — only the Orchestrator's instruction differs:

| Phase | Trigger | What the Extractor fetches | What the Loader does |
|---|---|---|---|
| **Bulk** | State store empty, or explicitly requested | Every row | Mostly creates |
| **Incremental** (fast pulse) | Every N minutes | `WHERE last_updated > watermark` | Upserts |
| **Deletions** (slow pulse) | Once daily | Only the `id` column | Archives (`active = False`) |

### Honest framing

A single well-organized script could produce identical output. The four-agent split buys
you enforced permission boundaries (the Transformer literally has no database handle), clean
fault attribution, and zero phase special-casing — but you do not strictly *need* agents or
LangGraph to sync two databases. The multi-agent architecture was partly a deliberate scope
choice for an internship deliverable. Know that before you defend it to a supervisor;
defend it on the boundaries-and-auditability argument, not on necessity.

---

## 2. Prerequisites

Verified working configuration (this is what it was developed and tested on):

| Component | Version / detail |
|---|---|
| OS | Windows 10 Pro (Linux/macOS should work; only `setup_odoo.ps1` is Windows-specific) |
| Python | 3.11.9 |
| MySQL | 8.x, on `127.0.0.1:3306` |
| Odoo | 19.0.20260724, Windows service `odoo-server-19.0`, on `:8069` |
| Odoo's PostgreSQL | service `PostgreSQL_For_Odoo`, `:5432` |
| OpenEduCat | branch `19.0`, cloned to `C:\Users\Shayan\odoo-addons\openeducat_erp` |
| Gemini | `gemini-3.1-flash-lite` via `langchain-google-genai` |

Python dependencies (`requirements.txt`):

```
langgraph>=0.2
langchain-google-genai>=2.0
mysql-connector-python>=9.0
apscheduler>=3.10
python-dotenv>=1.0
```

You will need your **own Gemini API key** — <https://aistudio.google.com/apikey>. The free
tier is enough (see §8 on caching), but the previous key is not in the repo and should not
be reused.

---

## 3. Setting up from zero

### 3.1 Clone and install

```bash
git clone https://github.com/SubhanSandhu312/OpenEdu-Orchestrator.git
cd OpenEdu-Orchestrator
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
```

### 3.2 Install MySQL and create the user

Install MySQL 8 Community Server ("Server only" is fine). Then create the account the
pipeline uses — **choose your own username and password**, and put them in `.env` (§3.5):

```sql
CREATE USER '<your_user>'@'localhost' IDENTIFIED BY '<your_password>';
GRANT ALL PRIVILEGES ON pieas_lms.* TO '<your_user>'@'localhost';
GRANT SELECT ON *.* TO '<your_user>'@'localhost';   -- needed for information_schema reads
FLUSH PRIVILEGES;
```

> The `information_schema` grant matters — the Extractor and Transformer both introspect
> column and foreign-key metadata at runtime (`connectors.py:60`). Without it, schema
> discovery silently returns nothing and mapping plans come out empty.

### 3.3 Install Odoo 19 and OpenEduCat

1. Install Odoo 19 (Windows installer registers the `odoo-server-19.0` service).
2. Clone OpenEduCat's `19.0` branch somewhere outside Odoo's program directory, e.g.
   `C:\Users\<you>\odoo-addons\openeducat_erp`.
3. Add that path to `addons_path` in `odoo.conf` and restart the service.

`setup_odoo.ps1` (in this repo, **run once, elevated**) automates step 3. It also fixes
two things that will otherwise cost you an afternoon:

- **Strips the UTF-8 BOM** from `odoo.conf`. PowerShell 5.1's `-Encoding utf8` writes a
  BOM, and Odoo cannot parse a config file that starts with one — it fails with an opaque
  error. The script writes UTF-8 *without* BOM explicitly.
- Normalizes `db_host` / `db_user` / `db_password` / `admin_passwd`.

Edit the paths at the top of that script for your machine before running it.

### 3.4 Create the Odoo database and install modules

1. Go to <http://localhost:8069>, create a database (the dev one is named `edu`).
2. Enable developer mode, then **Apps → Update Apps List**.
3. Install **`openeducat_core`** and **`openeducat_exam`**. These two are mandatory — the
   pipeline refuses to run without both (`run.py:96`).
4. Optional but used during demos: `openeducat_attendance`, `openeducat_library`,
   `openeducat_parent`.

> **If modules do not appear in Apps:** the addons path is wrong or Odoo did not restart.
> Check `odoo.conf`, restart the service, then Update Apps List again.

### 3.5 Create `.env`

`.env` is gitignored and **is not in the repo**. Create it in the project root:

```ini
GEMINI_API_KEY=<your-gemini-api-key>
GEMINI_MODEL=gemini-3.1-flash-lite

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=<the user you created in 3.2>
MYSQL_PASSWORD=<its password>
MYSQL_DATABASE=pieas_lms

ODOO_URL=http://localhost:8069
ODOO_DB=<your odoo database name>
ODOO_USER=<odoo login>
ODOO_PASSWORD=<odoo password>
```

`ODOO_USER` must be an Odoo *login* with write access to the OpenEduCat models.

> **Never commit `.env`.** It is already in `.gitignore` — keep it that way. Generate your
> own credentials rather than reusing any you were given verbally during handover, and
> rotate the Gemini key if it was ever shared.

---

## 4. First run and verification

```bash
python run.py check      # 1. verify MySQL + Odoo + Gemini all reachable
python run.py seed       # 2. build the pieas_lms schema + dummy data
python run.py bulk       # 3. migrate everything into Odoo
python run.py status     # 4. see what landed and where the watermarks are
```

`check` is your smoke test — run it first, always. It confirms MySQL connectivity, Odoo
authentication, that both required modules are installed, and that Gemini answers.

Expected after a clean `seed` + `bulk`: 6 departments, 8 courses, 10 batches, 14 subjects,
10 faculty, 20 students, 10 exams, 21 exam results — with `failed 0`.

### Proving the three phases actually work

```sql
-- fast pulse catches an update
UPDATE students SET phone = '+92-333-0000000' WHERE id = 3;

-- fast pulse catches an insert
INSERT INTO students (reg_no, first_name, last_name, email, phone, gender, course_id, batch_id)
VALUES ('PIEAS-25-EE-099','Daniyal','Chaudhry','daniyal@student.pieas.edu.pk',
        '+92-300-5551987','m', 3, 3);

-- slow pulse catches a deletion
DELETE FROM students WHERE id = 21;
```

Then `python run.py incremental` and `python run.py deletions`. The update and insert
appear in Odoo; the deleted student becomes **archived** (`active = False`), not destroyed.

> **`course_id` / `batch_id` must reference rows that exist.** The seed has courses 1–8 and
> batches 1–10. Using e.g. `course_id = 19` raises MySQL error 1452 (foreign key
> constraint). This is a real constraint in the source schema, not a bug.

Run `incremental` twice with no changes — the second run must process **zero rows**. That
is the watermark proving it prevents reprocessing.

---

## 5. Architecture

### 5.1 The LangGraph state machine (`graph.py`)

```
        ┌──────────────────┐
START ─▶│   orchestrator   │──(entities exhausted)──▶ END
        └────────┬─────────┘
                 │ dispatch
                 ▼
            extractor ──▶ transformer ──▶ loader
                 ▲                          │
                 └──────────────────────────┘
```

Four nodes, one shared `TypedDict` state (`graph.py:31`). Edges (`graph.py:162-170`):

- `START → orchestrator`
- `orchestrator →` **conditional**: `END` if `done`, else `extractor`
- `extractor → transformer → loader → orchestrator` (loop)

**The only conditional edge is at the Orchestrator.** Everything else is a straight line.
That is the architecture enforcing "one agent decides, three agents execute" — the workers
cannot branch, skip each other, or reorder themselves.

One graph invocation = one full lap over every entity in `config.ENTITIES`. `done` becomes
true when `idx >= len(plan)`. The recursion limit is set to `4 × len(ENTITIES) + 20`
(`graph.py:182`) — if you add many entities, this scales automatically.

### 5.2 The two rhythms

Updates and deletions need structurally different detection:

- **An update is easy.** `last_updated` moved past the watermark. A `WHERE` clause finds it.
- **A deletion is invisible.** No `WHERE` clause can match a row that no longer exists.

So the deletion scan fetches only the current `id` list and computes a **set difference**
against the ids the Orchestrator remembers as live (`agents.py:236`). What is in memory but
absent from the fetch is a "ghost". This is `LEFT JOIN … WHERE s.pieas_id IS NULL`
performed in Python.

Ghost detection is a full id scan, so it is cheaper than a full row fetch but still scales
with row count — hence "slow pulse", once daily.

### 5.3 Why a watermark and not a dirty flag

A boolean `sync_pending` column loses writes: if a row changes *again* while it is being
synced, resetting the flag to 0 after the first sync silently drops the second change. A
strictly advancing timestamp cannot lose a write that way (`state.py:69` refuses to move
backwards).

### 5.4 Why archive instead of delete

Destroying an ERP record takes grades and enrolment history with it, and a PIEAS-side
rename or temporary status change looks identical to a deletion from outside. `active =
False` preserves history and is reversible — if the row reappears, the next incremental
un-archives it (`graph.py:126`).

**Exception, by design:** line models such as `op.exam.attendees` have no `active` field,
so there is no archived state to move them into. The Loader detects this at runtime
(`connectors.py:152`) and unlinks instead, reporting `deleted` rather than `archived`.

### 5.5 Why XML-RPC and never raw PostgreSQL

Writing directly to Odoo's tables bypasses the ORM: no computed fields, no validation, no
related-record bookkeeping. You get records that look fine in the database and break Odoo's
business logic later, silently. XML-RPC forces every write through the same layer a human
clicking the UI would use.

---

## 6. Code map

```
config.py         schedule constants, entity routing, connection settings
state.py          SQLite state store — sync_mapping, watermarks, run log
connectors.py     PieasDB (MySQL, read-only) + OdooRPC (XML-RPC writes)
agents.py         the four agents + the Gemini helper and plan cache
graph.py          LangGraph wiring
run.py            CLI entry point and the APScheduler loop
sql/pieas_seed.sql   legacy schema + dummy data
plans/            cached LLM output (gitignored)
sync_state.db     the state store (gitignored)
```

### `config.py`

Everything you are likely to change is at the top.

| Line | Item | Notes |
|---|---|---|
| `:16` | `INCREMENTAL_EVERY_MINUTES = 15` | Fast pulse interval |
| `:21-22` | `DELETION_SCAN_HOUR/MINUTE = 2, 0` | Slow pulse — a **daily clock time**, not an interval |
| `:25` | `BATCH_SIZE = 200` | **Dead config — referenced nowhere.** See §10.3 |
| `:30-40` | `ENTITIES` | `(MySQL table, Odoo model)` pairs, **in dependency order** |
| `:46-59` | `MYSQL`, `ODOO` | Read from `.env` |
| `:61-64` | Gemini key and model | |
| `:70-73` | Paths | `STATE_DB`, `PLAN_CACHE`, `SEED_SQL` |
| `:77` | `ALLOWED_TABLES` | Derived from `ENTITIES`; the Extractor's SQL whitelist |

`ENTITIES` order matters: parents before children, so a child's foreign key can resolve to
a real Odoo id by the time it is synced.

### `agents.py`

| Symbol | Line | Purpose |
|---|---|---|
| `llm()` | `:44` | Lazily builds the Gemini client |
| `LLMUnavailable` | `:56` | Raised rather than returning a plausible-looking empty plan |
| `ask_json()` | `:60` | Gemini call + JSON extraction + retry/backoff, honours 429 retry hints |
| `cached()` | `:110` | Reason once, reuse forever. Only writes to disk if `validate` passes |
| **`Orchestrator`** | `:132` | |
| `.build_plan()` | `:140` | Surveys state, one Gemini call, then **overrides** the model's answer |
| `._default_step()` | `:218` | The deterministic fallback plan |
| `.detect_ghosts()` | `:236` | Set-difference ghost detection |
| `.finalize()` | `:248` | Advances the watermark — **only if `failed == 0`** |
| **`Extractor`** | `:268` | |
| `_SQL_FORBIDDEN` | `:263` | Regex blocking DML/DDL keywords |
| `.sql_for()` | `:274` | Generates + validates SQL, retries once with the rejection reason |
| `._default_sql()` | `:322` | Hand-written correct equivalent per phase |
| `._validate()` | `:330` | **The security boundary.** See below |
| **`Transformer`** | `:364` | |
| `.plan_for()` | `:370` | One Gemini call per entity → mapping plan |
| `._sanitize()` | `:435` | Drops anything the model invented; live schema is the authority |
| `._cast()` | `:508` | Type coercion |
| `.apply()` | `:528` | Deterministic per-row application of the cached plan |
| **`Loader`** | `:568` | |
| `._diagnose()` | `:575` | Asks Gemini how to recover from an Odoo rejection |
| `.load()` | `:602` | Create / write / archive loop |

**`Extractor._validate()` is the most security-critical function in the codebase.** No
generated SQL reaches MySQL without passing it. It rejects: empty strings, multiple
statements (any `;`), anything not starting with `SELECT`, forbidden keywords, tables
outside `ALLOWED_TABLES`, SQL that does not read the expected table, quoted `%s`
placeholders, and the wrong placeholder count for the phase. **Do not weaken this
function.** If you add a legitimate query shape it cannot express, extend it deliberately
and keep the read-only guarantee intact.

### `connectors.py`

`PieasDB` — read-only. `query()` (`:35`) parameterizes, `_normalize()` (`:43`) makes
`Decimal`/`datetime`/`bytes` XML-RPC-safe, `schema()` (`:60`) reads columns and foreign
keys from `information_schema`, `run_script()` (`:90`) is used only by `seed`.

`OdooRPC` — `writable_fields()` (`:136`) drops readonly fields, which is what stops the
pipeline trying to write stored-related fields like `op.exam.course_id` (derived from
`session_id`, raises if written). `archive()` (`:155`) falls back to `unlink` for models
with no `active` field. `exists()` (`:172`) uses `active_test: False` so archived records
still count as existing.

### `run.py`

| Command | Function | What it does |
|---|---|---|
| `seed` | `:21` | Runs `sql/pieas_seed.sql` — **drops and recreates `pieas_lms`** |
| `status` | `:32` | Per-entity active/archived counts and watermarks |
| `reset` | `:45` | Unlinks everything in `sync_mapping`, deletes state + plans. **Has a known bug — see §10.6** |
| `check` | `:80` | Connectivity smoke test |
| `schedule` | `:118` | APScheduler: interval job + cron job, blocking |
| `bulk`/`incremental`/`deletions` | `:153` | One-shot `graph.run(cmd)` |

---

## 7. The state store

SQLite, at `sync_state.db`, owned **exclusively** by the Orchestrator. It lives locally on
purpose: PIEAS stays read-only (zero modifications to the legacy source) and Odoo stays free
of sync bookkeeping.

Three tables (`state.py:31`):

**`sync_mapping`** — the id bridge *and* the ghost-detection index.
`(entity, pieas_id)` primary key → `odoo_model`, `odoo_id`, `row_hash`, `status`
(`active`/`archived`), `last_synced_at`.

The `row_hash` (`state.py:92`) is what makes re-runs cheap: if the hash of the transformed
values matches and the record is active, the row is skipped as genuinely unchanged
(`graph.py:121`).

**`sync_watermark`** — `entity` → `watermark`, `last_deletion_scan`.

**`sync_run`** — an append-only log of every entity-run: counts plus the Odoo error text
for failures. This is your audit trail; query it when something went wrong overnight.

```bash
sqlite3 sync_state.db "SELECT ran_at, phase, entity, created, updated, archived, failed, detail
                       FROM sync_run ORDER BY id DESC LIMIT 20;"
```

**Deleting `sync_state.db` makes the next run a full bulk migration.** It does not clean up
Odoo — you will get duplicates unless you reset Odoo too. See §10.6.

---

## 8. Where the LLM is used

Gemini reasons about **shape**, never about individual rows.

| Agent | Call site | What it decides | Frequency |
|---|---|---|---|
| Orchestrator | `agents.py:192` | Phase + extraction instruction per entity | Once per run |
| Extractor | `agents.py:297` | The SQL for an (entity, phase) | Once, then **cached to disk** |
| Transformer | `agents.py:424` | MySQL columns → Odoo fields mapping | Once per entity, **cached** |
| Loader | `agents.py:594` | Retry / drop-field / skip after a write rejection | Once per distinct error signature |

Per-row work is plain deterministic Python driven by cached plans. A warm bulk migration
costs roughly **one** Gemini call; a cold one (empty `plans/`) costs around 17.

### The safety model: the model proposes, the live schema decides

- **Generated SQL** must pass `_validate()` (§6) or it is rejected and regenerated once,
  then falls back to hand-written SQL.
- **Mapping plans** are filtered against Odoo's live `fields_get()`. Invented fields are
  dropped. Readonly fields are dropped.
- **Foreign keys the model missed** are recovered from `information_schema`
  (`agents.py:466`).
- **Literal values for relational fields are always rejected** (`agents.py:486`). This rule
  exists because of a real incident: a mapping plan once hardcoded the same `partner_id` for
  every student. Because `op.student` `_inherits` `res.partner`, that would have merged
  every student's identity into one record, each write overwriting the last. A relation may
  only ever be resolved through `sync_mapping`.
- **If Gemini is unreachable**, the Orchestrator, Extractor, and Loader all fall back to
  deterministic behaviour and the sync completes. The **Transformer has no fallback** by
  design (`agents.py:421`) — there is no deterministic way to guess that `reg_no` means
  `gr_no`, and an empty plan would happily create records with every required field missing.

### The Orchestrator overrides the model

`build_plan()` treats Gemini's answer as advice and then enforces the rules itself
(`agents.py:202-215`): an empty state store always forces `bulk`, and an explicitly
requested `bulk`/`incremental` always wins. This exists because Gemini was observed
reasoning its way to `incremental` for a non-empty entity even when `bulk` was requested —
silently turning a bulk run into a no-op.

### Cost control

The free tier caps requests **per day, per model**. Headline models are capped around
20/day, which a single cold run would exhaust — hence the `flash-lite` default. Because the
quota is per-model, switching `GEMINI_MODEL` in `.env` also grants a fresh allowance.

**Do not delete `plans/` casually.** It is the difference between 1 call and ~17.

---

## 9. Operations runbook

### Running the scheduler

```bash
python run.py schedule
```

Starts two APScheduler jobs (`run.py:125-132`): an interval job for the fast pulse and a
`CronTrigger` for the slow pulse. It runs one incremental immediately on startup so it is
not idle until the first tick, then blocks. `Ctrl+C` to stop.

### Daily checks

```bash
python run.py status     # counts + watermarks + last deletion scan
```

Watch for: watermarks not advancing (something is failing), `archived` counts growing
unexpectedly (source rows disappearing), any `failed` in the `sync_run` log.

### Changing cadence

Only the top of `config.py`. Remember `DELETION_SCAN_HOUR`/`MINUTE` is a **fixed daily
time**, not an interval.

### Running as a background service

Not currently configured. For production you would wrap `python run.py schedule` in a
Windows service (NSSM) or a systemd unit, with log redirection and restart-on-failure.

---

## 10. Known issues, bugs, and limitations

**Read this section before promising anything to a supervisor.**

### 10.1 🔴 Student course/batch enrollment is never synced

**This is the most significant functional gap in the project.**

OpenEduCat does not store a student's course on `op.student`. It has no `course_id` or
`batch_id` field at all. Enrollment lives in a separate line model, **`op.student.course`**
(`student_id`, `course_id` *(required)*, `batch_id`, `roll_number`).

The cached mapping plan maps the source `students.course_id` onto `course_detail_ids`,
which is a **one2many**. `Transformer.apply()` assigns a scalar Odoo id to it. Odoo silently
ignores this — no error, no exception, the record is created successfully — and the
enrollment is simply never written.

**Verified impact:** of 21 synced students, **20 have no course enrollment whatsoever**.
(The one exception was created manually during demo preparation.) `students.batch_id` is not
mapped at all.

**Root cause** is §10.2 below. **Fixing it properly** requires teaching the pipeline to
write child line records — see §12.3.

> If you demo this project, do not claim student enrollment syncs. It does not.

### 10.2 🔴 `_sanitize()` does not type-check relation targets

`agents.py:457-463` accepts any relation whose target is in `writable`, without checking
that the target is actually a `many2one`. A `one2many` or `many2many` target passes
validation and then receives a scalar id, which Odoo ignores.

**Minimal fix** — require `many2one`:

```python
# agents.py, in _sanitize(), the relations loop
if (col not in src_cols or tgt not in writable
        or ref not in config.ALLOWED_TABLES
        or writable[tgt]["type"] != "many2one"):
    out["dropped"].append(f"relation {col}->{tgt}")
    continue
```

This makes the failure **visible** (the relation lands in `dropped`) instead of silent. It
does not by itself make enrollment work — for that see §12.3.

After changing this, delete `plans/map_students.json` to force a re-plan.

### 10.3 `BATCH_SIZE` is dead configuration

Declared at `config.py:25`, documented in the README, **referenced nowhere in the code**.
Either wire it up (§10.4) or delete it. Leaving it is misleading.

### 10.4 The Loader is unbatched — one XML-RPC round trip per record

`agents.py:606` loops record-by-record. At ~100 rows this is fine; at real institutional
scale it will be painfully slow. Odoo's `create` accepts a **list** of vals dicts and
creates them all in one call.

Batching is the single biggest available performance win, but note the tradeoff: currently a
failure isolates to one row, and the per-row `_diagnose()` retry logic depends on that. A
batched implementation needs to fall back to per-row on batch failure to preserve error
granularity.

### 10.5 🔒 The Loader sends real record values to Gemini on failure

`agents.py:580-592` includes the actual failed record's field values — names, emails, phone
numbers — in the diagnosis prompt. Every other Gemini call sends schema metadata only.

**Disclose this proactively** to whoever owns data governance. If it is not acceptable,
fail closed: replace `self._diagnose(...)` with `{"action": "skip"}`, or redact `vals`
before building the prompt.

### 10.6 🔴 `run.py reset` leaves orphaned `res.partner` records

`op.student` and `op.faculty` `_inherits` from `res.partner`. **Odoo does not cascade-delete
the parent partner** when you unlink the child. `cmd_reset` (`run.py:45`) unlinks only the
child records.

**Symptom:** the next `bulk` fails for every student and faculty with
`<Fault 2: 'Email must be unique per partner!'>` — the old partner still holds the email.

**Manual cleanup** (adapt the email list):

```python
from connectors import OdooRPC
o = OdooRPC()
emails = [...]  # the emails of records you deleted
partners = o.execute('res.partner', 'search_read', [['email','in',emails]], fields=['id'])
o.execute('res.partner', 'unlink', [p['id'] for p in partners])
```

**Proper fix:** before unlinking a partner-backed model, read its `partner_id`, unlink the
child, then unlink the partner. Worth building into `cmd_reset` directly.

### 10.7 A third-party OpenEduCat module can block deletion

`openeducat_parent`'s `unlink()` override calls `child_ids.remove(record.user_id.id)`, which
raises `ValueError` for a student that has `parent_ids` set but whose `user_id` is not in
that parent's `child_ids`. This blocks deleting such students entirely.

Workaround: exclude the affected students from batch unlinks. It is a bug in OpenEduCat, not
in this project.

### 10.8 Odoo referential integrity blocks deletion generally

Records referenced by other models refuse to unlink ("Another model is using the record").
Cleaning up requires discovering and removing blockers in dependency order. Budget time for
this whenever you reset.

### 10.9 `run_script()` splits naively on `;`

`connectors.py:96` splits the seed file on semicolons. It works for the current seed file
but would break on a semicolon inside a string literal or a stored-procedure body. If you
extend the seed SQL, keep that in mind.

### 10.10 Watermark advances from fetched row values

`finalize()` (`agents.py:248`) takes `max(last_updated)` across fetched rows. If the MySQL
clock skews, or rows are inserted with backdated `last_updated` values, a change could fall
below the watermark and be missed. Not a problem with MySQL's automatic
`ON UPDATE CURRENT_TIMESTAMP`, but worth knowing if anyone starts setting that column
manually.

### 10.11 Not tested at real scale or against the real PIEAS schema

Everything was validated against a synthetic ~99-row fixture across 8 tables. The real PIEAS
schema will have more tables, more columns, messier data, and edge cases this has never
seen. Treat the mapping plans as a starting point, not a finished migration.

---

## 11. Gotchas that will waste your day

**Odoo 19 domain syntax via `OdooRPC.execute()`.** The wrapper passes `*args` as the
positional list, so the domain is *already* wrapped. Passing `[[]]` raises
`ValueError: Domain() invalid item in domain: []`.

```python
o.execute('op.student', 'search_count', [])                    # ✅ all records
o.execute('op.student', 'search_read', [['name','=','X']], fields=['name'])   # ✅
o.execute('op.student', 'search_count', [[]])                  # ❌ raises
```

To include archived records, pass `context={'active_test': False}`.

**`mysql-connector-python` 26.x removed `multi=True`.** This broke `run.py seed` with
`TypeError: execute() got an unexpected keyword argument 'multi'`. Fixed in commit `9aba2d4`
by split-executing statements. If you pin an older connector, the old code would work — but
do not revert.

**`information_schema` returns UPPERCASE column names in MySQL 8.** Every query against it
must alias explicitly (`connectors.py:68`), or `column_name` comes back as `COLUMN_NAME` and
schema discovery silently produces nothing.

**`CronTrigger` fires at the next *future* occurrence.** If you set `DELETION_SCAN_HOUR`/
`MINUTE` to a time that has already passed today, APScheduler schedules it for **tomorrow**
and nothing appears to happen. Always set it a few minutes ahead, and confirm against the
banner the scheduler prints on startup.

**Concurrent pulses interleave their output.** If the slow pulse fires in the same minute as
a fast pulse, both run in separate threads and their `print()` output tangles together on
one terminal, plus you will see `maximum number of running instances reached (1)` as ticks
get skipped. This is cosmetic, not corruption — but during a live demo it looks alarming.
Space the two apart.

**Python buffers stdout when not attached to a TTY.** If you redirect the scheduler to a
log file and see nothing, use `python -u run.py schedule`.

**Backgrounding with `&` does not survive a tool/shell session.** Use a proper background
mechanism, and verify the process actually died when you think you killed it —
`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select ProcessId, CommandLine`
shows full command lines on Windows.

**Deleting `plans/` costs ~17 Gemini calls.** On a constrained free tier that can exhaust
your daily quota in one go.

**`seed` drops the entire `pieas_lms` database.** It is `DROP DATABASE IF EXISTS`. Never run
it against anything you care about.

---

## 12. How to extend it

### 12.1 Add a new entity

One line in `config.ENTITIES` — **in dependency order**, parents before children:

```python
ENTITIES = [
    ("departments",   "op.department"),
    ...
    ("attendance",    "op.attendance.line"),   # new
]
```

The agents discover its columns, foreign keys, and target fields at runtime. `ALLOWED_TABLES`
updates automatically. On the next run the Extractor and Transformer each make one Gemini
call for it and cache the result.

**Then verify the generated plan** — open `plans/map_<entity>.json` and check every
relation target is a `many2one` and that `unmet_required` is empty.

### 12.2 Point an entity at a different Odoo model

Change the model string in `ENTITIES`, then delete that entity's cached plan
(`plans/map_<entity>.json`) so the Transformer re-reasons against the new model's schema.

### 12.3 Support child line models (fixes §10.1)

This is the highest-value piece of real work outstanding. Enrollment (`op.student.course`)
is the motivating case, but the same pattern covers any parent→line relationship.

Suggested approach:

1. Add an optional `lines` section to the mapping plan schema: which source columns feed
   which child model, and which field links back to the parent.
2. In `Loader.load()`, after creating or updating the parent record, upsert the child line
   with the parent's Odoo id, resolving the child's own relations through `sync_mapping` the
   same way the parent's are.
3. Record the child in `sync_mapping` under its own synthetic entity key (e.g.
   `students_enrollment`) so re-runs update rather than duplicate.
4. Apply the §10.2 type check first, so a one2many can never silently absorb a scalar again.

For enrollment specifically, the child record needs `student_id` (parent), `course_id`
(required, resolved from `sync_mapping['courses']`), and `batch_id` (resolved from
`sync_mapping['batches']`).

### 12.4 Swap Gemini for another model

`llm()` (`agents.py:44`) is a thin wrapper. Swap the LangChain chat class and the config
values; the prompts themselves are provider-agnostic. Nothing else changes.

### 12.5 Point at a different source database

`PieasDB` is the only MySQL surface. Implementing the same `query()` / `schema()` interface
against another backend is enough for the rest of the pipeline — but note the Extractor's
generated SQL is MySQL-flavoured (`%s` placeholders, backtick quoting), so `_default_sql()`
and `_validate()` would need dialect awareness.

---

## 13. Debugging playbook

| Symptom | Where to look |
|---|---|
| Everything fails at startup | `python run.py check` — isolates MySQL vs Odoo vs Gemini |
| `Email must be unique per partner!` | Orphaned `res.partner` rows — §10.6 |
| Records fail with "Another model is using the record" | Referential integrity — §10.8 |
| Watermark not advancing | An entity has `failed > 0`; check `sync_run`. This is deliberate — failures are retried, not skipped |
| A field is silently not written | Check `plans/map_<entity>.json` → `dropped` list, and check the target's type in `fields_get` |
| `Gemini daily quota exhausted` | Switch `GEMINI_MODEL` in `.env` (quota is per-model) or wait for reset |
| Bulk run does nothing | Was the Orchestrator overridden correctly? Check the printed plan — every entity should say `bulk` |
| Scheduler appears frozen | Check `DELETION_SCAN_*` did not schedule for tomorrow (§11); use `python -u` |
| Duplicates in Odoo | `sync_mapping` lost its rows but Odoo kept the records. Reset both together |

**Inspect the state store directly:**

```bash
sqlite3 sync_state.db "SELECT * FROM sync_mapping WHERE entity='students' LIMIT 10;"
sqlite3 sync_state.db "SELECT ran_at,phase,entity,failed,detail FROM sync_run
                       WHERE failed>0 ORDER BY id DESC LIMIT 10;"
```

**Inspect a cached plan:** `plans/map_<entity>.json` shows exactly what the Transformer will
do to every row — `fields`, `relations`, `computed`, `defaults`, `dropped`,
`unmet_required`. This is usually the fastest way to understand a mapping problem.

---

## 14. Production readiness gaps

Be honest about these with anyone who asks whether this is deployable.

| Gap | Detail |
|---|---|
| **Secrets management** | Credentials sit in a plaintext `.env`. No rotation, no vault. |
| **PII to a third party** | §10.5 — record values reach Gemini on write failures. |
| **No automated backup** | Nothing snapshots Odoo before a run. A bad mapping plan can write a lot of wrong records quickly. |
| **No monitoring/alerting** | Failures land in `sync_run` and nowhere else. Nobody is paged. |
| **No test suite** | There are no automated tests at all. Every validation so far has been manual. |
| **Not run as a service** | The scheduler is a foreground process. |
| **Unbatched writes** | §10.4 — will not scale to real enrollment numbers. |
| **Untested against real data** | §10.11. |
| **Single-tenant assumptions** | One source database, one Odoo target, hardcoded in `.env`. |

---

## 15. Suggested roadmap

In the order I would actually do them:

1. **Fix the relation type check** (§10.2). Small, makes hidden failures visible.
2. **Implement child line models** (§12.3) so student enrollment actually syncs. This is the
   difference between "migrates students" and "migrates students *usefully*".
3. **Fix `cmd_reset` to clean up partners** (§10.6). You will reset many times; make it not
   hurt.
4. **Write a test suite.** `Transformer.apply()` is a pure function over a plan and a row —
   trivially unit-testable and the highest-risk logic in the codebase. `_validate()` deserves
   adversarial tests.
5. **Batch the Loader** (§10.4), with per-row fallback on batch failure. Wire up or remove
   `BATCH_SIZE`.
6. **Validate against the real PIEAS schema** — request a de-identified dump before promising
   any timeline.
7. **Run as a service** with proper logging and restart-on-failure.
8. **Then** consider the nice-to-haves: parallel entity processing, conflict detection for
   manual Odoo edits, a dashboard over `sync_run`.

### Questions to ask whoever owns the data

- Can I see the real PIEAS schema, or a de-identified copy?
- Is there a staging or read-replica MySQL I should point at instead of production?
- Does PIEAS's data-retention/privacy policy permit any external LLM API call at all?
- Who issues service-account credentials for MySQL read access and Odoo API access?
- What is the actual data volume? It determines whether the unbatched write path is viable.

---

## 16. Glossary

| Term | Meaning |
|---|---|
| **Watermark** | The strictly advancing `last_updated` high-water mark per entity. The fast pulse asks "what changed after T?", never "what is dirty?" |
| **Ghost** | A row present in `sync_mapping` as active but absent from PIEAS — i.e. deleted at the source. |
| **Fast pulse** | The frequent incremental sync catching inserts and updates. |
| **Slow pulse** | The daily deletion scan hunting ghosts. |
| **Plan** | Cached LLM output in `plans/` — either generated SQL or a field mapping. |
| **Entity** | A `(MySQL table, Odoo model)` pair from `config.ENTITIES`. |
| **`_inherits`** | Odoo's delegation mechanism. `op.student` delegates to `res.partner`; unlinking the student does **not** delete the partner. |
| **Archive** | Setting `active = False` — Odoo's soft delete. Reversible, preserves history. |
| **Upsert** | Create if unmapped, update if mapped. Keyed on the source system's primary key, never on the phase. |

---

## Appendix: other files in this repo

| File | Purpose |
|---|---|
| `README.md` | Shorter conceptual overview — the "what and why" |
| `DEMO.md` | Live demo runbook, including how to reset state for a clean bulk migration |
| `SPEAKER_NOTES.md` | Per-slide presentation notes plus anticipated supervisor Q&A |
| `agentic_pieas_erp_sync.pptx` | 10-slide presentation deck |
| `report/report.tex`, `report/report.pdf` | Formal LaTeX report (build with XeLaTeX) |
| `setup_odoo.ps1` | One-time elevated Odoo/OpenEduCat configuration (gitignored; Windows-specific) |

**Not in the repo** (gitignored, you must create or generate them): `.env`,
`sync_state.db`, `plans/`, `__pycache__/`.
