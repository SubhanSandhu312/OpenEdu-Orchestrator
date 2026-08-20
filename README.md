# PIEAS → OpenEduCat: Agentic ERP Synchronization

A 4-agent [LangGraph](https://langchain-ai.github.io/langgraph/) system that keeps an
Odoo/OpenEduCat ERP continuously in sync with a legacy PIEAS LMS database — a source
with **no API, no webhooks, and no ability to announce its own changes**.

Because the source cannot push, the system must hunt. It does so on two rhythms, and
routes each kind of record into the correct OpenEduCat module.

> **New to this project / taking over maintenance?** Start with **[HANDOFF.md](HANDOFF.md)** —
> full environment setup from zero, a code map, the operations runbook, known bugs, and the
> suggested roadmap. This README is the conceptual overview; `HANDOFF.md` is what you need to
> actually own the thing.

---

## The four agents

| Agent | Role | Holds state? |
|---|---|---|
| **Orchestrator** | The only decision-maker. Owns the state store and the schedule, picks each entity's phase, writes the Extractor's instructions, compares fetched data against memory, advances the watermark. | **Yes — exclusively** |
| **Extractor** | The pure fetcher. The only agent that touches PIEAS. Takes an instruction, returns rows. Makes no comparisons. | No |
| **Transformer** | The pure function. Maps the PIEAS schema onto OpenEduCat models. Identical logic in every phase. | No |
| **Loader** | The writer. Create, update, or archive via XML-RPC. | No |

```
                    ┌──────────────────┐
        START ─────▶│   orchestrator   │────(entities exhausted)───▶ END
                    └────────┬─────────┘
                             │ dispatch
                             ▼
                        extractor ──▶ transformer ──▶ loader
                             ▲                          │
                             └──────────────────────────┘
```

## The three phases — one pipeline

| | Trigger | Extractor instruction | Loader operation |
|---|---|---|---|
| **Bulk migration** | Empty state store | Fetch everything | Mostly creates |
| **Incremental (fast pulse)** | Every N minutes | `WHERE last_updated > watermark` | Upserts |
| **Deletion scan (slow pulse)** | Daily | Fetch ids only | Archive (`active = False`) |

**Why a watermark and not a dirty flag.** A boolean `sync_pending` column fails: if a row
changes *again* while it is being synced, blindly resetting the flag to 0 silently drops
the second change. A strictly advancing timestamp cannot lose a write.

**Why a separate deletion scan.** `WHERE last_updated > watermark` physically cannot see a
row that no longer exists. So the slow pulse pulls the current id list and compares it
against the ids the Orchestrator remembers as live — the set difference is the ghost list.
This is the in-memory form of `LEFT JOIN … WHERE s.pieas_id IS NULL`.

**Why archive instead of delete.** Destroying the ERP record would take grades and
enrolment history with it, and a PIEAS-side rename or temporary status change would look
identical to a deletion. `active = False` preserves historical truth and is reversible —
if the row reappears, the next incremental sync un-archives it.

The Loader checks at runtime whether a model can actually be archived. Most can. Line
models such as `op.exam.attendees` have no `active` field at all, so there is no archived
state to move them into and the record is removed instead — reporting a mark for an exam
sitting that no longer exists would be worse. The run summary counts the two separately.

**Why XML-RPC and not direct PostgreSQL.** Writing straight to Odoo's tables skips the ORM:
no computed fields, no validation, no related-record bookkeeping. You get half-formed
records that fail silently later. XML-RPC ships with every self-hosted Odoo and enforces
every business rule.

## Where the LLM actually thinks

Gemini does schema reasoning, not row-by-row grunt work:

| Agent | Gemini call | Frequency |
|---|---|---|
| Orchestrator | Plan phases + write extraction instructions | Once per run |
| Extractor | Author the SQL from the instruction | Once per (entity, phase) — **cached to disk** |
| Transformer | Map MySQL columns onto the live `fields_get()` | Once per entity — **cached to disk** |
| Loader | Triage an Odoo rejection | Once per distinct error |

Per-row execution is deterministic Python driven by those cached plans. A bulk migration of
thousands of rows costs a handful of model calls, not thousands, and is reproducible.

**The model proposes; the live schema decides.** Every plan is sanitized before use:

- Generated SQL must be a single `SELECT`, against a whitelisted table, with the right
  placeholder count — or it is rejected and a deterministic fallback is used.
- Mapping plans are filtered against Odoo's `fields_get()`, and **readonly fields are
  dropped**. This is what stops the pipeline writing `op.exam.course_id`, which is a
  stored-related field derived from `session_id` and would raise.
- Foreign keys the model missed are recovered from `information_schema`.
- If Gemini is unreachable, every agent falls back to a deterministic plan and the sync
  still completes.

## Entity routing

Declared in `config.py`, in dependency order so foreign keys resolve to real Odoo ids:

| MySQL table | OpenEduCat model | Module |
|---|---|---|
| `departments` | `op.department` | core |
| `courses` | `op.course` | core |
| `batches` | `op.batch` | core |
| `subjects` | `op.subject` | core |
| `faculty` | `op.faculty` | core |
| `students` | `op.student` | core |
| `exams` | `op.exam` | **Examination** |
| `exam_results` | `op.exam.attendees` | **Examination** |

Adding an entity is one line in `config.ENTITIES` — the agents discover its columns,
foreign keys, and target fields at runtime.

---

## Setup

```bash
pip install -r requirements.txt
```

Fill in `.env` (MySQL, Odoo, and `GEMINI_API_KEY`), then:

```bash
python run.py check
```

## Usage

```bash
python run.py seed          # create the PIEAS MySQL database + dummy data
python run.py bulk          # Phase 1 - migrate everything
python run.py incremental   # one fast pulse
python run.py deletions     # one slow pulse
python run.py schedule      # run both pulses forever
python run.py status        # what is synced, and where the watermarks are
python run.py reset         # remove everything this pipeline wrote to Odoo
```

`reset` only touches records listed in `sync_mapping`, so OpenEduCat's own bundled sample
records are left alone. Use it to re-run a clean demo from scratch.

## Changing the schedule

Top of `config.py`, nothing else:

```python
INCREMENTAL_EVERY_MINUTES = 15      # fast pulse - catches updates
DELETION_SCAN_HOUR   = 2            # slow pulse - catches deletions
DELETION_SCAN_MINUTE = 0
BATCH_SIZE = 200
```

## Files

| File | Purpose |
|---|---|
| `config.py` | Schedule, entity routing, connections |
| `state.py` | `sync_mapping` + watermarks (SQLite, Orchestrator-owned) |
| `connectors.py` | MySQL reads; Odoo XML-RPC writes |
| `agents.py` | The four agents |
| `graph.py` | LangGraph state machine |
| `run.py` | CLI + scheduler |
| `sql/pieas_seed.sql` | Legacy schema + dummy data |

State lives in `sync_state.db`; cached LLM plans live in `plans/`. Delete `plans/` to make
the agents re-reason about the schema. Deleting `sync_state.db` resets everything and the
next run becomes a full bulk migration.

## Troubleshooting

**`Gemini daily quota exhausted`** — the free tier caps requests *per day, per model*, and
the headline models are capped at 20/day, which a single cold run would burn through. The
default `gemini-3.1-flash-lite` has a far higher allowance. Because the quota is per-model,
switching `GEMINI_MODEL` in `.env` also gives you a fresh allowance.

Cached plans mean a warm run costs **one** Gemini call. Deleting `plans/` forces the agents
to re-reason about the schema and costs roughly 17 calls, so don't do it casually.

**A sync reports `failed`** — `python run.py status` shows what is mapped, and the
`sync_run` table in `sync_state.db` keeps the Odoo error for every failed record. The
watermark is deliberately *not* advanced for an entity with failures, so the next run
retries those rows rather than skipping past them.

## Proving it works

```bash
python run.py seed && python run.py bulk        # everything lands in Odoo
```

Then, in MySQL:

```sql
UPDATE students SET first_name='Changed' WHERE id=1;   -- python run.py incremental
INSERT INTO exams (...) VALUES (...);                  -- python run.py incremental
DELETE FROM exam_results WHERE id=1;                   -- python run.py deletions
```

Each change appears in the matching OpenEduCat module; the deleted row is archived,
not destroyed. Running `incremental` twice with no changes processes zero rows —
the watermark holds.
