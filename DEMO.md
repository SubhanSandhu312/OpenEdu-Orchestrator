# Live Demo Script — PIEAS → OpenEduCat Sync

Order: **PIEAS source (MySQL Workbench) → run the pipeline (terminal) → OpenEduCat (browser)**.
Every step below names the exact agent/function it's exercising, so you can pull up the
code if someone asks "show me."

Do NOT touch student id 1 (Muhammad Hamza Yousaf) or faculty "Ahmed Raza" — those back
two working portal logins (student + faculty), with real enrollment, attendance, and a
library card already built out. Everything else in Odoo has been deliberately cleared
so you can run a genuine **bulk migration live** (see "State reset" below). Use the ids
given in each step instead of picking your own.

---

## 0. Before you start

Have three windows ready to alt-tab between:
1. **MySQL Workbench**, connected, `pieas_lms` schema open
2. **Terminal**, `cd` into the project folder
3. **Browser**, `http://localhost:8069`, logged in as `edu` / `eduu`

```bash
python run.py check
```
Shows MySQL, Odoo, and Gemini all reachable in one shot — a clean way to open the demo.
This exercises `config.py`'s connection settings and confirms `openeducat_core` +
`openeducat_exam` are installed.

**State reset, already done:** every synced Odoo record was deleted except one row per
entity (`pieas_id=1`) — Hamza's student record, Ahmed's faculty record, and the
course/batch/department they belong to (BS Physics / BSPHY 2022-2026 / Dept. of Physics
and Applied Mathematics). Their `sync_mapping` rows were kept; every other mapping row was
deleted along with the Odoo record it pointed to. `python run.py status` right now shows
exactly **1 active row** in departments, courses, batches, faculty, and students, and
**0** in subjects, exams, and exam_results. Nothing in MySQL was touched — this only
cleared Odoo and the Orchestrator's own memory of it.

---

## 1. Show the source: "PIEAS cannot speak"

In Workbench, run:
```sql
SHOW TABLES;
SELECT id, reg_no, first_name, last_name, email, phone, last_updated
FROM students ORDER BY id;
```
Narrate: this is the entire legacy PIEAS LMS — 8 plain tables, no API, no webhooks, a
`last_updated` column MySQL maintains automatically and nothing else. Point out there is
no `sync_pending` flag, no trigger, nothing added to this schema for our sake — the
constraint from the design (Section 1.2 of the report) is real, not simulated.

---

## 2. Live bulk migration — Phase 1, from (almost) nothing

```bash
python run.py status
```
Show the near-empty state first: 1 row in five entities, 0 in three. This is the "state
store empty" condition from Section 1.2 — the Orchestrator is about to see this and
decide "bulk" for everything.

```bash
python run.py bulk
```
Narrate while it runs: the `Orchestrator` (`agents.py:132`) surveys every entity, tells
the `Extractor` (`agents.py:262`) to paginate every row of each PIEAS table, the
`Transformer` (`agents.py:358`) maps each row onto its Odoo model (schema plan cached
per entity — point out the first entity is slower than the rest, that's the one live
Gemini call), and the `Loader` (`agents.py:562`) writes every one of them over XML-RPC.

You'll see ~6 departments, ~7 courses, ~9 batches, 14 subjects, ~9 faculty, ~20 students,
~11 exams, and ~20 exam results all get created fresh. **One exception, worth pointing
out live:** Hamza's student row and Ahmed's faculty row show up as `updated`, not
`created` — because their `sync_mapping` row was deliberately kept, so the Loader
(`graph.py:117-129`) looks them up, finds they already exist, and does a `write()`
instead of creating a duplicate. That's the exact mechanism that makes bulk *and*
incremental share one code path with zero special-casing.

Switch to the browser: **Education → Students**, **Education → Exams** — the full list
just appeared from PIEAS, not typed in by hand. Log in as `hamza.yousaf@student.pieas.edu.pk`
/ `student123` on the portal to show his login, enrollment, and attendance survived the
whole thing untouched.

---

## 3. Turn the human out of the loop

**This is the actual point of the demo.** Everything above still had *you* naming the
phase (`bulk`) on the command line — that's you being the orchestrator, not the code.
From here on, you type exactly one command, then you only ever touch MySQL. The
Orchestrator decides everything else itself: which entity needs which phase, and when
each pulse runs.

**Before you start this section**, temporarily speed up the cadence in `config.py` so
both pulses fire inside a live demo instead of over real hours — the top of the file is
the "one thing meant to be edited," this is exactly that:
```python
INCREMENTAL_EVERY_MINUTES = 1                    # was 15
DELETION_SCAN_HOUR   = <current hour>            # e.g. 14
DELETION_SCAN_MINUTE = <two minutes from now>    # e.g. 32
```
(`DELETION_SCAN_HOUR`/`MINUTE` is a fixed daily time, not an interval — set it a couple
minutes ahead of whenever you're actually presenting.)

**In the terminal, start it and leave it running, visible, for the rest of the demo:**
```bash
python run.py schedule
```
It immediately runs one fast pulse (so it's not idle for 15 minutes for the *first* one),
then blocks, ticking on its own. Narrate: this is `config.py`'s cadence driving
`graph.run("incremental")` on a timer and `graph.run("deletions")` on a cron trigger —
nobody names a phase from here on; the Orchestrator decides that per entity, every time,
from `state.py`'s watermarks.

**Now switch entirely to Workbench** and make both kinds of change, back to back, without
touching the terminal at all:
```sql
UPDATE students
SET phone = '+92-333-7654321'
WHERE id = 2;                              -- Maryam Nawaz

INSERT INTO students (reg_no, first_name, last_name, email, phone, gender, course_id, batch_id)
VALUES ('PIEAS-25-EE-002', 'Ayesha', 'Malik',
        'ayesha.malik@student.pieas.edu.pk', '+92-300-5551234', 'f', 3, 4);

DELETE FROM students WHERE id = 21;        -- PIEAS-25-CS-999, a scratch test row
```

**Now just wait, narrating, watching the terminal window.** Within a minute the fast
pulse fires on its own and you'll see it fetch `WHERE last_updated > watermark`, mapping
and writing Maryam's update and Ayesha's new record — the `Loader` upserts: `write()` for
Maryam, `create()` for Ayesha. At the minute you set, the slow pulse fires on its own:
the Extractor pulls only current ids, the Orchestrator diffs them against
`known_ids('students')` (`state.py:129` — the LEFT JOIN / ghost-detection logic from
Section 3.1 of the report), and the Loader archives Subhan Khan — `active = False`,
never deleted.

**In the browser**, refresh **Education → Students**: Maryam's phone is updated, Ayesha
Malik is there, and under Filters → Archived, Subhan Khan is greyed out but present, his
exam results still fully intact. None of this required you to type `incremental` or
`deletions` — you only ever touched the source.

`Ctrl+C` to stop the scheduler afterward, and put `config.py`'s two constants back
(`15`, `2`, `0`) once the demo's done.

---

## If someone asks to see the code

| What you just showed | Where it lives |
|---|---|
| Orchestrator (phase decision, watermark, scheduling) | `agents.py:132` — class `Orchestrator` |
| Extractor (only agent touching PIEAS, generates SQL) | `agents.py:262` — class `Extractor` |
| Transformer (schema mapping, cached per entity) | `agents.py:358` — class `Transformer` |
| Loader (create/update/archive via XML-RPC) | `agents.py:562` — class `Loader` |
| The LangGraph wiring (4 nodes, conditional router) | `graph.py:45` — `build()` |
| State store (`sync_mapping`, watermarks) | `state.py` |
| Schedule cadence (the one thing meant to be edited) | top of `config.py` |
