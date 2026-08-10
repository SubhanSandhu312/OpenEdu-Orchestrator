# Mapping-Authoring Tool -- Design Scope

## Why this exists

The original plan (per the internship's direction) was an LLM-assisted
Transformer for "messy/ambiguous field mapping." Testing bulk migration
against a real local Odoo 19.0 + OpenEduCat 19.0 instance
(2026-08-07..08-10) produced real evidence that complicates that premise:
every actual mismatch we hit was a lookup, not a judgment call --

| Mismatch found | Nature |
|---|---|
| `roll_number` -> `gr_no` | Rename, visible directly in `fields_get`'s `string` label |
| `gender` codes differ between `op.student` (`m`/`f`/`o`) and `op.faculty` (`male`/`female`) on the *same instance* | Value-domain mismatch, visible directly in `fields_get`'s `selection` list |
| `op.faculty.gender` has no source value at all (PIEAS has no gender field for faculty) | Data-availability gap -- no mapping fixes a missing source value |
| `department`/`batch_year` have no plain-field equivalent on `op.student` (relational via `course_detail_ids`) | Structural/relational modeling gap -- needs ORM knowledge (create/link `op.department`, enrollment records), not fuzzy matching |

None of these needed live natural-language reasoning per record. They
needed **schema introspection** (`fields_get`, already implemented in
`OdooXmlRpcClient`) plus a static lookup table -- exactly the kind of thing
that should stay deterministic, testable, and auditable, not be re-decided
by a model on every sync run.

## Where an LLM genuinely helps: authoring, not runtime

Onboarding a *new* university source system still means a human sitting
down once to work out "their `student_no` is our `gr_no`," across
potentially dozens of fields. An LLM given both schemas (structured,
complete, via introspection -- not guesswork) is well-suited to proposing
that first draft for a human to review. That's a **design-time tool**, run
once per new source system or target schema change -- not a component in
the live sync path.

## Non-goals

- **Not a runtime component.** Never called during an actual `run_cycle`.
  Cost/latency of the LLM call is a non-issue here specifically because it
  runs rarely, not per-record.
- **Not authoritative.** Output is always a *draft* requiring human review
  before it's compiled into an actual `transform_fn`.
- **Not a relational-mapping solver.** Fields needing FK/relational
  modeling (department, batch/enrollment) are flagged as `unmapped` with an
  explanatory note, not guessed at. That's a separate, deterministic
  ORM-aware problem.
- **Not a data-sourcing fix.** Target-required fields with no source
  equivalent are flagged (`unmapped_required_target_fields`), not
  defaulted/fabricated -- matches the project's existing "never invent
  data" stance (see faculty gender, above).

## Pipeline

```
source Pydantic model (models.py)      target fields_get (OdooXmlRpcClient)
            |                                       |
            v                                       v
   extract_source_schema()             extract_target_schema()
            |                                       |
            +-------------------+--------------------+
                                 v
                     build_mapping_prompt()
                                 v
                        propose_mapping()  <-- the one seam needing an
                                 |              LLM API call; not wired yet
                                 v              (no ANTHROPIC_API_KEY in
                       MappingProposal (draft)  this environment to verify
                                 |              against -- see below)
                        [human review + edit]
                                 v
                      approved mapping config (JSON)
                                 |
                        compile_mapping()   <-- fully deterministic, no LLM
                                 v
                    transform_fn(entity_type, record) -> dict
                                 |
                                 v
              graph.py's run_cycle(..., transform_fn=...)  <-- today's
                                                                 injection
                                                                 point
```

## Data shapes (see `models.py`)

`FieldMappingEntry`: one source field's disposition --
`direct` (rename or passthrough), `value_map` (enum/selection translation),
`external_id` (routed to the client's `ir.model.data` handling, same as
`pieas_id` today), or `unmapped` (flagged, not guessed).

`MappingProposal`: `entity_type`, `target_model`, a list of
`FieldMappingEntry`, and `unmapped_required_target_fields` -- target fields
that are `required=True` per `fields_get` but have no candidate source
field at all (this is exactly how the faculty-gender gap would have been
surfaced automatically, instead of discovered by a failed live write).

## What's implemented now vs. deferred

**Implemented and tested** (`mapping_authoring.py`):
- `extract_source_schema(entity_type)` -- reads the existing
  `PIEAS_MODEL_FOR_ENTITY` Pydantic models already in `models.py`. No new
  source-schema infrastructure needed; every source system this project
  supports already defines one of these.
- `extract_target_schema(client, target_model)` -- wraps the same
  `fields_get` call used to hand-diagnose today's real mismatches.
- `compile_mapping(config)` -- turns an approved static config into a
  plain `transform_fn`, verified against **today's actual hand-verified
  real-target mapping** (the one that successfully migrated 60 students to
  the live instance) as a golden-output regression test.

**Deferred, clearly marked** (`_call_llm` in `mapping_authoring.py`):
- The actual Anthropic API call. No `ANTHROPIC_API_KEY` is available in
  this environment to verify a live call against, so `_call_llm` raises
  `NotImplementedError` with a pointer back to this doc rather than
  shipping unverified prompt-engineering as if it were tested. Wiring this
  up is a small, mechanical step once a key is available -- structured
  output against the existing `MappingProposal` pydantic schema (Claude's
  tool-use / structured-output mode), using `extract_source_schema` +
  `extract_target_schema` + a handful of sample records as the prompt
  context.

## Review/approval workflow

1. `propose_mapping(entity_type, target_model)` writes a draft to
   `mappings/<entity_type>_<source>_draft.json`.
2. A human reviews it, edits as needed (especially the
   `unmapped_required_target_fields` list -- those need a real decision,
   not a mapping), and saves the approved version to
   `mappings/<entity_type>_<source>.json`.
3. `compile_mapping()` loads the approved file and produces the
   `transform_fn` passed to `run_cycle`. The LLM is never in this path.

## Testing strategy

- `extract_source_schema`/`extract_target_schema`: standard unit tests
  against known model shapes (the latter against a stub client, not a live
  Odoo instance -- keeps the suite fast and independent of any running
  service).
- `compile_mapping`: golden-output test using today's real, live-verified
  mapping as ground truth, not just internal self-consistency.
- `propose_mapping`/`_call_llm`: once wired, evaluate against the same
  ground truth -- does the LLM's draft correctly flag `gr_no`, the gender
  encoding mismatch, and the two unmapped-relational fields? That's a
  concrete, evidenced eval, not a vague aspiration.
