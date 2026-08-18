# Speaker Notes — Agentic PIEAS ERP Synchronization

Split between two presenters:

- **Presenter A — "The Problem & The Architecture"**: Slides 1–4. Sets up why this is
  hard and how the system is shaped to solve it.
- **Presenter B — "How It Actually Runs & Proof It Works"**: Slides 5–10. Goes into the
  operational mechanics, the LLM strategy, and verification.

Both of you should skim the other's section once — supervisor questions don't always
land on the slide currently on screen, and either of you should be able to field a
question that strays across the handoff. The Q&A section at the end is likewise split by
who's more likely to get asked what, but treat that as a starting assignment, not a wall.

Notes are written to be said aloud, not read verbatim — use them to sound fluent, not
scripted.

---
---

# PRESENTER A — Slides 1–4

## Slide 1 — Title

"This project builds a four-agent system that keeps PIEAS's legacy student records
system continuously synchronized with OpenEduCat — the Odoo-based ERP the institute is
migrating to. The core challenge, which the next slide gets into, is that the legacy
system can't tell us when something changes. Everything here is built and tested against
a real MySQL instance and a real Odoo 19 installation — nothing in this deck is
simulated."

---

## Slide 2 — Synchronizing a System That Cannot Speak

"PIEAS's current LMS is a standalone system with no API, no webhooks — nothing that lets
it announce a change. Meanwhile OpenEduCat is a modern, API-rich ERP. That asymmetry is
the entire design problem: the *target* system has to actively go find out what changed
at the source, because the source will never tell it. That's the question this whole
project answers: how do you synchronize a system that cannot speak?"

*If asked why not just add a webhook to PIEAS:* "Because PIEAS is legacy and explicitly
off-limits to modify — it's currently in production use, and touching it risks breaking
that. Everything downstream of this constraint — the watermark design, the read-only
Extractor, all of it — exists because we can only ever pull from PIEAS, never be told by
it."

*If asked what else shaped the design besides the no-modification rule:* "Two more things
mattered as much: it has to stay correct if it crashes mid-run — restart must neither skip
a change nor duplicate one, which is what drove the watermark approach — and it has to
write through Odoo's own API rather than around it, so OpenEduCat's validation and
computed fields stay intact."

---

## Slide 3 — A Specialized Multi-Agent Architecture

"Rather than one script that does everything, this is four agents with a strict rule:
one agent decides, three agents execute. The Orchestrator is the only one that holds
memory or makes a decision. Extractor, Transformer, and Loader are stateless — handed an
instruction, they do exactly that and nothing more. This separation is what let us find
and fix real bugs cleanly later — when something went wrong, we always knew which of the
four agents was responsible, because their responsibilities never overlap. The dashed
lines from the Orchestrator down to each worker are the control signal; the solid arrows
along the bottom are the actual data flow from PIEAS through to OpenEduCat."

---

## Slide 4 — Four Agents, Four Clear Jobs

Give each a one-line personality, not just a definition:

- **Orchestrator** — "the brain. Reads state, decides bulk vs. incremental vs. deletion
  scan, per entity, every run. It's the only one of the four that holds memory or makes a
  decision — the check-schedule, instruct-extractor, evaluate-state, command-pipeline
  cycle repeats once per entity, every run."
- **Extractor** — "the only agent allowed to touch PIEAS at all. Given an instruction, it
  authors and validates a read-only SQL query."
- **Transformer** — "a pure function — PIEAS row in, Odoo-shaped record out. Same logic
  whether it's bulk or incremental."
- **Loader** — "the only agent that writes. Create, update, or archive over XML-RPC."

*If asked why LangGraph specifically:* see Q&A below.

**Handoff line to Presenter B:** "So that's the shape of the system and why it looks the
way it does. [B] is going to walk through how it actually behaves when it runs, the
choices around the LLM, and the proof that it works."

---
---

# PRESENTER B — Slides 5–10

## Slide 5 — Three Phases, One Pipeline

"This is the payoff of the one-agent-one-job design [A] just walked through: bulk
migration, incremental sync, and deletion detection are the *same* four agents — only the
Orchestrator's instruction changes. Bulk fetches everything. Incremental fetches only
what changed since the watermark. Deletion scan fetches only ids, to check what's
missing. No separate codepath for any of the three."

---

## Slide 6 — Incremental Sync Needs Two Distinct Rhythms

"Since PIEAS can't push, we have to poll — but updates and deletions need fundamentally
different polling strategies. Catching an *update* is easy: `last_updated` moved forward,
compare it to a watermark. Catching a *deletion* is structurally different — a `WHERE`
clause can't find a row that isn't there anymore. So the fast pulse (every 15–60 minutes)
handles updates and inserts, and a separate, cheaper-but-less-frequent slow pulse (once
daily) hunts for deletions by comparing the full list of ids we remember against what
PIEAS actually returns right now."

*Call out the anti-pattern box if there's time:* "We deliberately didn't use a boolean
'sync_pending' flag — if a row changes twice before it's synced, resetting that flag
after the first sync silently drops the second change. A monotonically advancing
timestamp can't lose a write that way."

---

## Slide 7 — Archiving Missing Records Preserves Historical Truth

"When the slow pulse confirms a row is gone from PIEAS, we don't delete the Odoo record —
we archive it, `active = False`. Two reasons: first, a student's grades and enrollment
history need to survive even after they leave — you don't want a `NOT NULL` foreign key
suddenly pointing at nothing. Second, it's reversible — if PIEAS's deletion turns out to
be a rename or a temporary status flip, and the row comes back, the next sync just
un-archives it instead of losing history permanently."

---

## Slide 8 — Reasoning Once, Executing Deterministically

"This is the LLM strategy, and it's deliberately conservative. Gemini is never asked to
make a decision about an individual row. It reasons about *shape*: which phase to plan,
how to map a schema once per entity, how to write one SQL query once per entity/phase.
All of that gets cached, so a bulk migration of thousands of rows costs a handful of
model calls, not thousands — and it produces identical output on every re-run, which
matters a lot for something that has to be demonstrably reliable."

"Just as important: every LLM output is sanitized before it's trusted. Generated SQL is
parsed and rejected if it's anything but one read-only SELECT on the right table. Mapping
plans are filtered against Odoo's live schema, so a hallucinated or non-writable field
gets silently dropped instead of breaking a write. If Gemini's unreachable, every agent
falls back to a deterministic plan — the sync still completes."

*If asked for a concrete example of the sanitizer catching something real:* "Early on,
the Transformer's mapping plan hardcoded the same contact-record id for every student —
because OpenEduCat students inherit from Odoo's partner model, that would have silently
merged every student's identity into one. The fix was a blanket rule in the sanitizer:
literal values are never accepted for a relational field, full stop — a relation can only
ever be resolved through our own mapping table. That's the kind of thing this slide's
'sanitized before use' line is protecting against."

---

## Slide 9 — Verified End-to-End Against Live Systems

"Every one of these was run against the actual MySQL source and actual Odoo instance —
99 rows across 8 tables migrated with zero failures, a live update and a live insert both
correctly reflected, a new exam correctly routed into the Examination module rather than
some generic table, a deletion correctly archived rather than destroyed, and running
incremental sync twice back-to-back correctly processed zero rows the second time —
proving the watermark actually prevents reprocessing."

---

## Slide 10 — Closing

"Specialized agents, strict state control, complete data integrity. If there's time for
future work: parallelizing independent entities, detecting conflicting manual edits on
the Odoo side, and a lightweight dashboard over the sync run history that already gets
logged today."

---
---

# Anticipated Supervisor Questions

## For Presenter A — Architecture & design choices

**Q: Why four agents instead of one script with functions?**
A: The separation is what makes each piece auditable and safely reusable across three
phases. The Loader, for instance, doesn't know or care whether it's mid-bulk-migration or
mid-incremental-sync — it just executes create/write/archive based on what it's handed.
That's what let bulk, incremental, and deletion detection share one pipeline with zero
special-casing, and it's what made root-causing real bugs fast — the responsibility for
any given failure was never ambiguous.

**Q: Why LangGraph specifically, and not just a linear script?**
A: LangGraph gives the state machine a conditional router as a first-class thing — the
Orchestrator's phase decision determines which node runs next per entity, not a chain of
if/else. It also gives clean visibility into the state passed between agents, which
mattered for debugging a real phase-decision bug found later, where the model would
sometimes override an explicitly requested phase.

**Q: Why not just poll MySQL binlog / use change data capture (CDC) instead of
timestamp watermarks?**
A: CDC tools (Debezium, etc.) are the more "production-grade" answer long-term, but they
typically require enabling binlog replication and often a Kafka-adjacent pipeline —
meaningfully more infrastructure than an intern-scoped project justifies, and it would
mean touching PIEAS's database configuration, which brushes against the "zero
modification" constraint. A `last_updated` watermark achieves the same correctness
guarantee (nothing is missed, nothing is reprocessed) with an existing column and no new
infrastructure.

**Q: Why archive deletions instead of actually deleting them?**
A: Two reasons on the slide — preserving historical data (grades, attendance) that other
records still reference, and reversibility, since PIEAS renames or temporary status
changes can look identical to a real deletion from the outside. Archiving is the
conservative default; a true hard-delete could be layered on top later for records
confirmed safe to purge (e.g., after some retention period), but that wasn't in scope.

**Q: Why XML-RPC instead of just writing to Odoo's Postgres database directly?**
A: Direct writes skip Odoo's computed fields, validation constraints, and related-record
bookkeeping entirely. A half-written record that looks fine in the database but breaks
Odoo's own business logic would fail silently, often much later. XML-RPC forces every
write through the same ORM layer a human clicking through the UI would use.

## For Presenter B — LLM usage & reliability

**Q: Isn't using an LLM in a data pipeline risky — what if it hallucinates?**
A: That's why the design treats every LLM output as an untrusted proposal, never a
final answer. Generated SQL is parsed and rejected unless it's exactly one read-only
SELECT against the correct table. Mapping plans are filtered against Odoo's live schema —
anything non-writable or hallucinated gets silently dropped. The clearest proof this
matters: a real case where the model hardcoded a foreign key that would have merged every
student's identity into one. The fix wasn't "trust it more carefully," it was a blanket
rule: literal values are never accepted for relational fields, full stop.

**Q: What happens if Gemini is down or the API key is invalid?**
A: Every agent has a deterministic fallback. If the LLM call fails, the Orchestrator uses
a rule-based phase decision, the Extractor falls back to a hand-written default SQL
query, and the sync still completes — it just runs without the LLM's help for that call.
Nothing blocks on Gemini being available.

**Q: How much does this cost to run, in API calls?**
A: Very little, by design. Because mapping plans and SQL queries are cached to disk after
the first successful generation, a warm re-run of even a large bulk migration costs a
single-digit number of Gemini calls — not one call per row. The free tier's
20-requests-per-day cap was actually enough once caching was in place; it was only a
problem before the caching logic existed.

**Q: Could you swap Gemini for a different model?**
A: Yes — the LLM helper is a thin wrapper (`agents.py`'s `llm()`/`ask_json()`), so
swapping the model class or provider is a localized change, not a rewrite of the agent
logic. The prompts themselves are provider-agnostic.

## For Presenter B — Testing & correctness

**Q: How thoroughly is this actually tested — is any of this simulated?**
A: None of it. Every result on the verification slide came from running the real CLI
against a live MySQL instance seeded with realistic PIEAS-shaped data and a live Odoo 19 /
OpenEduCat installation — not mocks, not a demo environment built to look convincing.

**Q: What happens if the sync crashes halfway through a run?**
A: The watermark for an entity only advances after the Loader reports zero failures for
that batch — so a crash or a partial failure just means the next run re-fetches
everything from the last confirmed-good watermark. Nothing is skipped, and because the
per-row logic is an upsert keyed on the source id, nothing gets duplicated either.

**Q: How do you know it doesn't create duplicate records?**
A: Every row's create-vs-update decision is based on a lookup in `sync_mapping`, keyed by
the source system's own primary key — never on the phase (bulk/incremental) being run.
That's actually why the bulk migration can be safely re-run without creating duplicates
of already-synced records, which was demonstrated directly during development.

## For Presenter B — Scope & limitations

**Q: Does this give students a way to see their own grades or attendance online?**
A: Not out of the box — that's a genuine limitation worth being upfront about.
OpenEduCat's Community Edition is a staff-facing backend tool; none of its modules
(attendance, library, exams) expose a student self-service portal. What this project
delivers is the data pipeline that gets PIEAS's data correctly and continuously into
OpenEduCat — a real self-service student portal would be a separate, additional piece of
custom development, not a config option.

**Q: What's the biggest known limitation of this design?**
A: The deletion scan is a full-table id comparison — cheap compared to fetching every
column, but it still scales with row count, so at a much larger institution than PIEAS
it would eventually need to run less frequently or be optimized further. It's also
currently sequential per entity rather than parallel, which is on the future-work list.

**Q: Could this handle a much larger dataset than PIEAS's?**
A: The core design would hold — the watermark and ghost-detection logic don't depend on
row count for correctness, only for how long a full scan takes. Parallelizing independent
entities (mentioned in future work) would be the first thing to add before scaling up
significantly.

## Either presenter — practical / demo-specific

**Q: Why does the demo need a scheduler at all — why not just run the sync manually
when needed?**
A: Because manually picking "run bulk now" or "run incremental now" makes the *person*
the orchestrator, not the code. The actual claim this project makes is that the
Orchestrator decides which phase to run, per entity, from state and a clock — without a
human naming the phase. The scheduler is what proves that: start it once, and from then
on you only ever touch the source data; the system reacts on its own.

**Q: What would you change if you were starting over?**
A: Build the LLM-output sanitization layer first, before any agent logic — every real bug
hit traced back to trusting a model output a beat too early. Also design the state
store's protected/reference data handling (courses, batches, departments that downstream
data depends on) more deliberately from the start, rather than discovering the dependency
chain by hitting Odoo's own referential-integrity errors one at a time.

**Q: Who worked on which part?**
A: *(Fill in honestly based on your actual split — e.g., "[A] focused on the
architecture and agent design, [B] on the LLM integration and verification," or however
it actually broke down.)*
