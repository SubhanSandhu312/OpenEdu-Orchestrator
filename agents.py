"""
The four agents.

  Orchestrator   the ONLY decision-maker. Owns the state store and the schedule,
                 decides each entity's phase, writes the instructions, evaluates
                 fetched data against memory, and advances the watermark.
  Extractor      the only agent that touches PIEAS. Takes an instruction,
                 returns rows. Makes no comparisons.
  Transformer    a pure function. PIEAS schema -> OpenEduCat schema. Identical
                 logic in every phase.
  Loader         the writer. Create, update, or archive, over XML-RPC.

Where Gemini actually thinks:

  * Orchestrator -- once per run, to plan phases and write extraction instructions.
  * Extractor    -- once per (entity, phase), to author the SQL. Cached to disk.
  * Transformer  -- once per entity, to map MySQL columns onto the live Odoo
                    fields_get(). Cached to disk.
  * Loader       -- only when Odoo rejects a write, to triage the failure.

Per-row work is then plain deterministic Python driven by those cached plans, so
a bulk migration of thousands of rows costs a handful of model calls, not
thousands, and produces identical output every time.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from langchain_google_genai import ChatGoogleGenerativeAI

import config
import state
from connectors import PieasDB, OdooRPC

# ═══════════════════════════════════════════════════════════ Gemini helper ══

_llm = None


def llm():
    global _llm
    if _llm is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY missing from .env")
        _llm = ChatGoogleGenerativeAI(
            model=config.GEMINI_MODEL,
            google_api_key=config.GEMINI_API_KEY,
        )
    return _llm


class LLMUnavailable(RuntimeError):
    """Gemini could not be reached or would not produce usable JSON."""


def ask_json(prompt: str, attempts: int = 4):
    """Ask Gemini for JSON.

    Raises LLMUnavailable rather than returning something plausible-looking.
    An earlier version returned an empty fallback plan here, which was far worse
    than failing: an empty mapping plan still "works", it just writes records
    with every required field missing. Callers now decide explicitly whether a
    deterministic fallback is safe for their particular job.
    """
    last = None
    for i in range(attempts):
        try:
            text = llm().invoke(prompt).content
            if isinstance(text, list):  # some SDK versions return content parts
                text = "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                               for p in text)
            m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
            if m:
                text = m.group(1)
            start = min((i2 for i2 in (text.find("{"), text.find("[")) if i2 != -1),
                        default=-1)
            if start == -1:
                raise ValueError(f"no JSON in response: {text[:200]}")
            end = max(text.rfind("}"), text.rfind("]"))
            return json.loads(text[start:end + 1])
        except Exception as e:
            last = e
            msg = str(e)
            if "429" in msg or "quota" in msg.lower() or "exhausted" in msg.lower():
                # Honour the server's own retry hint when it gives one.
                m = re.search(r"retry in ([\d.]+)s", msg, re.I)
                delay = float(m.group(1)) + 1 if m else 5 * (i + 1)
                if i < attempts - 1:
                    print(f"   [llm] rate limited, retrying in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise LLMUnavailable(
                    f"Gemini daily quota exhausted for model '{config.GEMINI_MODEL}'. "
                    f"Set a different GEMINI_MODEL in .env or wait for the reset."
                ) from e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise LLMUnavailable(f"{type(last).__name__}: {last}")


def _cache_path(name: str) -> str:
    os.makedirs(config.PLAN_CACHE, exist_ok=True)
    return os.path.join(config.PLAN_CACHE, name)


def cached(name: str, build, validate=None):
    """Reason once, reuse forever. Delete plans/ to force a re-think.

    A result is only written to disk if `validate` accepts it, so a bad run can
    never poison the cache permanently.
    """
    p = _cache_path(name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            val = json.load(fh)
        if validate is None or validate(val):
            return val
        os.remove(p)                      # cached plan is unusable; re-think it
    val = build()
    if validate is None or validate(val):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(val, fh, indent=2)
    return val


# ═════════════════════════════════════════════════════════════ ORCHESTRATOR ══

class Orchestrator:
    """The only agent that makes decisions or holds memory."""

    def __init__(self, pieas: PieasDB):
        self.pieas = pieas

    # ---- (1) check schedule / (2) write the instructions ----------------

    def build_plan(self, run_phase: str) -> list[dict]:
        """One Gemini call per run: decide each entity's phase and instruction."""
        survey = []
        for table, model in config.ENTITIES:
            wm = state.get_watermark(table)
            try:
                pending = self.pieas.query(
                    f"SELECT COUNT(*) n FROM `{table}` WHERE last_updated > %s", (wm,)
                )[0]["n"]
                total = self.pieas.query(f"SELECT COUNT(*) n FROM `{table}`")[0]["n"]
            except Exception:
                pending, total = -1, -1
            survey.append({
                "entity": table, "odoo_model": model,
                "state_store_empty": state.is_empty(table),
                "watermark": wm, "rows_changed_since_watermark": pending,
                "rows_in_pieas": total,
            })

        fallback = [self._default_step(s, run_phase) for s in survey]

        if run_phase == "deletions":
            # A ghost hunt is mechanical: always a full id scan, every entity.
            return fallback

        prompt = f"""You are the Orchestrator of a data synchronization pipeline moving
records from a legacy MySQL system (PIEAS LMS) into an Odoo/OpenEduCat ERP.

You decide, per entity, which phase to run and what instruction to hand the
Extractor agent. The Extractor can only read from MySQL and only from the entity's
own table.

Rules you must follow:
- If state_store_empty is true, the phase MUST be "bulk": nothing has ever been
  migrated, so fetch every row.
- Otherwise the phase MUST be "incremental": fetch only rows whose `last_updated`
  is strictly greater than the watermark.
- The requested run phase is "{run_phase}". If it is "bulk" or "incremental",
  honour it for every entity unless the state_store_empty rule above forces "bulk".
- The instruction is a short natural-language sentence for the Extractor
  describing exactly which rows to fetch. Mention the watermark comparison for
  incremental. Never mention writing, deleting, or any other table.

Current state:
{json.dumps(survey, indent=2)}

Return ONLY a JSON array, one object per entity, in the same order:
[{{"entity": "...", "phase": "bulk|incremental", "instruction": "...", "reason": "..."}}]"""

        # Safe to fall back here: the deterministic plan is exactly what the
        # rules above would produce anyway.
        try:
            plan = ask_json(prompt)
        except LLMUnavailable as e:
            print(f"   [orchestrator] {e}\n   [orchestrator] using deterministic plan")
            plan = fallback

        # The model advises; the state store and the caller's explicit request
        # decide. Never let it skip a bulk, and never let it second-guess an
        # explicitly requested phase -- Gemini has been observed reasoning its
        # way to "incremental" for a non-empty entity even when "bulk" was the
        # requested run_phase, which silently turned a bulk run into a no-op.
        by_entity = {p.get("entity"): p for p in plan if isinstance(p, dict)}
        final = []
        for s, fb in zip(survey, fallback):
            p = by_entity.get(s["entity"], fb)
            if s["state_store_empty"]:
                p["phase"] = "bulk"
            elif run_phase in ("bulk", "incremental"):
                p["phase"] = run_phase
            elif p.get("phase") not in ("bulk", "incremental"):
                p["phase"] = fb["phase"]
            p["entity"] = s["entity"]
            p["odoo_model"] = s["odoo_model"]
            p.setdefault("instruction", fb["instruction"])
            final.append(p)
        return final

    @staticmethod
    def _default_step(s: dict, run_phase: str) -> dict:
        if run_phase == "deletions":
            phase, instr = "deletions", (
                f"Fetch only the id column of every row currently in {s['entity']}.")
        elif run_phase == "bulk" or s["state_store_empty"]:
            phase, instr = "bulk", (
                f"Fetch every row of {s['entity']}, ordered by id.")
        else:
            phase, instr = "incremental", (
                f"Fetch every row of {s['entity']} whose last_updated is strictly "
                f"greater than the watermark, ordered by last_updated.")
        return {"entity": s["entity"], "odoo_model": s["odoo_model"],
                "phase": phase, "instruction": instr, "reason": "deterministic default"}

    # ---- (3) evaluate state --------------------------------------------

    @staticmethod
    def detect_ghosts(entity: str, live_ids: list[int]) -> list[int]:
        """The slow pulse's LEFT JOIN, in memory.

        A row that no longer exists in PIEAS cannot be found by any WHERE clause
        -- it is simply absent. So we compare the ids we still believe are live
        against the ids PIEAS actually returned. What is in our memory but not in
        the fetch is a ghost.
        """
        return sorted(set(state.known_ids(entity)) - set(int(i) for i in live_ids))

    # ---- (4) advance the watermark -------------------------------------

    @staticmethod
    def finalize(entity: str, phase: str, rows: list[dict], stats: dict):
        """Only ever called after the Loader reports success."""
        if phase == "deletions":
            state.record_deletion_scan(entity)
        elif rows and stats.get("failed", 0) == 0:
            newest = max((str(r.get("last_updated") or "") for r in rows), default="")
            state.advance_watermark(entity, newest)
        state.log_run(phase, entity, stats.get("created", 0), stats.get("updated", 0),
                      stats.get("archived", 0), stats.get("failed", 0),
                      "; ".join(stats.get("errors", []))[:2000])


# ════════════════════════════════════════════════════════════════ EXTRACTOR ══

_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|"
    r"call|handler|load_file|outfile|dumpfile|information_schema|mysql)\b", re.I)


class Extractor:
    """The pure fetcher. The only agent that touches PIEAS."""

    def __init__(self, pieas: PieasDB):
        self.pieas = pieas

    def sql_for(self, entity: str, phase: str, instruction: str) -> str:
        def build():
            schema = self.pieas.schema(entity)
            cols = ", ".join(f"{c['column_name']} {c['column_type']}"
                             for c in schema["columns"])
            prompt = f"""Write ONE MySQL SELECT statement that satisfies this instruction.

Instruction: {instruction}
Table: {entity}
Columns: {cols}

Hard rules:
- Exactly one SELECT statement. No semicolon. No comments. No subqueries on other tables.
- Read only from the table `{entity}`.
- If the instruction mentions a watermark, compare `last_updated` with the
  placeholder %s  (literally two characters: percent, s). Use exactly one placeholder.
- If the instruction asks only for ids, select only the `id` column.
- Otherwise select all columns with *.

Return ONLY JSON: {{"sql": "SELECT ..."}}"""
            attempt = prompt
            for _ in range(2):
                try:
                    out = ask_json(attempt)
                    sql = (out.get("sql") or "").strip().rstrip(";")
                    self._validate(sql, entity, phase)
                    return {"sql": sql, "source": "gemini"}
                except ValueError as e:
                    # Hand the rejection back and let it correct itself.
                    print(f"   [extractor] rejected generated SQL ({e}); retrying")
                    attempt = f"{prompt}\n\nYour previous answer was rejected: {e}. Fix it."
                except LLMUnavailable as e:
                    print(f"   [extractor] {str(e)[:90]}")
                    break
            # The default is a correct hand-written equivalent. Cache it too, so
            # a model that keeps getting this wrong stops costing a call per run.
            print(f"   [extractor] using default SQL for {entity}/{phase}")
            return {"sql": self._default_sql(entity, phase), "source": "fallback"}

        def usable(v: dict) -> bool:
            try:                       # re-check cached SQL, not just fresh SQL
                self._validate(v.get("sql", ""), entity, phase)
                return True
            except ValueError:
                return False

        return cached(f"sql_{entity}_{phase}.json", build, validate=usable)["sql"]

    @staticmethod
    def _default_sql(entity: str, phase: str) -> str:
        if phase == "deletions":
            return f"SELECT id FROM {entity}"
        if phase == "bulk":
            return f"SELECT * FROM {entity} ORDER BY id"
        return f"SELECT * FROM {entity} WHERE last_updated > %s ORDER BY last_updated"

    @staticmethod
    def _validate(sql: str, entity: str, phase: str):
        """No amount of clever prompting reaches MySQL without passing this."""
        if not sql:
            raise ValueError("empty")
        if ";" in sql:
            raise ValueError("multiple statements")
        if not sql.lstrip().lower().startswith("select"):
            raise ValueError("not a SELECT")
        if _SQL_FORBIDDEN.search(sql):
            raise ValueError("forbidden keyword")
        tables = set(re.findall(r"\b(?:from|join)\s+`?(\w+)`?", sql, re.I))
        if not tables <= config.ALLOWED_TABLES:
            raise ValueError(f"table not allowed: {tables - config.ALLOWED_TABLES}")
        if entity not in tables:
            raise ValueError(f"does not read {entity}")
        # The driver quotes parameters itself. A hand-quoted '%s' becomes
        # '''2026-01-01 00:00:00''' and is a syntax error.
        if re.search(r"""['"]\s*%s\s*['"]""", sql):
            raise ValueError("placeholder must not be quoted")
        n = sql.count("%s")
        if phase == "incremental" and n != 1:
            raise ValueError(f"incremental needs exactly one %s placeholder, got {n}")
        if phase != "incremental" and n != 0:
            raise ValueError(f"{phase} takes no parameters, got {n}")

    def fetch(self, entity: str, phase: str, instruction: str) -> list[dict]:
        sql = self.sql_for(entity, phase, instruction)
        params = (state.get_watermark(entity),) if phase == "incremental" else ()
        return self.pieas.query(sql, params)


# ══════════════════════════════════════════════════════════════ TRANSFORMER ══

class Transformer:
    """Pure function: PIEAS row -> OpenEduCat values. No I/O decisions."""

    def __init__(self, pieas: PieasDB, odoo: OdooRPC):
        self.pieas, self.odoo = pieas, odoo

    def plan_for(self, entity: str, model: str) -> dict:
        def build():
            schema = self.pieas.schema(entity)
            writable = self.odoo.writable_fields(model)
            slim = {k: {"type": v["type"], "required": v.get("required", False),
                        "relation": v.get("relation"),
                        "selection": [s[0] for s in (v.get("selection") or [])][:12]}
                    for k, v in writable.items()}
            src = {c["column_name"]: c["column_type"] for c in schema["columns"]}

            prompt = f"""Map a legacy MySQL table onto an Odoo/OpenEduCat model.

SOURCE table `{entity}` columns:
{json.dumps(src, indent=2)}

SOURCE foreign keys (column -> referenced table):
{json.dumps(schema['foreign_keys'], indent=2)}

TARGET Odoo model `{model}`, WRITABLE fields only:
{json.dumps(slim, indent=2)}

Produce a mapping plan as JSON with these keys:

"fields":    {{"<source_column>": {{"target":"<odoo_field>",
                                    "cast":"str|int|float|bool|date|datetime",
                                    "values":{{"<src>":"<odoo_selection_value>"}} }} }}
             Direct column-to-field mappings. "values" is optional and only for
             Selection fields. Omit `id` and `last_updated` -- those are sync
             bookkeeping, not business data.

"relations": {{"<source_fk_column>": {{"target":"<odoo_many2one_field>",
                                       "entity":"<referenced source table>"}} }}
             Foreign keys. The value will be resolved to the real Odoo id later;
             you only say which field it feeds.

"computed":  {{"<odoo_field>": "<python format string over source columns>"}}
             e.g. {{"name": "{{first_name}} {{last_name}}"}}

"defaults":  {{"<odoo_field>": <literal>}}
             Used only when nothing else supplies a REQUIRED field.

Rules:
- Only ever name fields that appear in the WRITABLE list above.
- Every field marked required:true must be filled by fields, relations,
  computed, or defaults.
- If the model stores a person (it has first_name/last_name), also set "name"
  via "computed", because the underlying partner record needs it.
- Prefer semantically correct matches over name similarity.

Return ONLY the JSON object."""

            # No fallback is possible here. There is no deterministic way to
            # guess that `reg_no` means `gr_no`; an empty plan would happily
            # create records with every required field missing.
            plan = ask_json(prompt)
            return self._sanitize(plan, writable, schema, model, entity)

        return cached(f"map_{entity}.json", build, validate=self._usable)

    @staticmethod
    def _usable(plan: dict) -> bool:
        """A plan is only worth keeping if it maps something and misses nothing."""
        return bool(plan.get("fields") or plan.get("computed")) \
            and not plan.get("unmet_required")

    @staticmethod
    def _sanitize(plan: dict, writable: dict, schema: dict, model: str, entity: str) -> dict:
        """Drop anything the model invented, then backfill from the FK metadata.

        This is what makes an LLM safe here: its output is a proposal, and the
        live Odoo schema is the authority.
        """
        src_cols = {c["column_name"] for c in schema["columns"]}
        out = {"model": model, "fields": {}, "relations": {},
               "computed": {}, "defaults": {}, "dropped": []}

        for col, spec in (plan.get("fields") or {}).items():
            tgt = (spec or {}).get("target")
            if col in ("id", "last_updated") or col not in src_cols:
                continue
            if tgt not in writable:
                out["dropped"].append(f"field {col}->{tgt}")
                continue
            out["fields"][col] = {"target": tgt,
                                  "cast": spec.get("cast", "str"),
                                  "values": spec.get("values") or {}}

        for col, spec in (plan.get("relations") or {}).items():
            tgt = (spec or {}).get("target")
            ref = (spec or {}).get("entity")
            if col not in src_cols or tgt not in writable or ref not in config.ALLOWED_TABLES:
                out["dropped"].append(f"relation {col}->{tgt}")
                continue
            out["relations"][col] = {"target": tgt, "entity": ref}

        # Any FK the model missed, recovered from information_schema.
        for col, ref_table in schema["foreign_keys"].items():
            if col in out["relations"] or ref_table not in config.ALLOWED_TABLES:
                continue
            if col in writable and writable[col]["type"] == "many2one":
                out["relations"][col] = {"target": col, "entity": ref_table}

        for tgt, tmpl in (plan.get("computed") or {}).items():
            if tgt in writable and isinstance(tmpl, str):
                out["computed"][tgt] = tmpl
            else:
                out["dropped"].append(f"computed {tgt}")

        for tgt, val in (plan.get("defaults") or {}).items():
            if tgt not in writable:
                continue
            # A hardcoded id for a relational field is never right. Left alone it
            # is actively destructive: a literal partner_id on a model that
            # _inherits res.partner binds every record to one partner, and each
            # write overwrites the previous record's name. Real relations must
            # come from "relations" and be resolved through sync_mapping.
            if writable[tgt]["type"] in ("many2one", "one2many", "many2many"):
                out["dropped"].append(f"default {tgt} (relational literal)")
                continue
            out["defaults"][tgt] = val

        covered = ({s["target"] for s in out["fields"].values()} |
                   {s["target"] for s in out["relations"].values()} |
                   set(out["computed"]) | set(out["defaults"]))
        # Required relational fields are not our problem to solve. Either they
        # are real foreign keys -- already recovered from information_schema
        # above -- or they are _inherits parent links such as op.student.partner_id,
        # which Odoo creates automatically from the values we pass. We refuse to
        # invent ids for them either way, so flagging them would only make every
        # plan look permanently invalid and force a needless LLM call per run.
        out["unmet_required"] = sorted(
            k for k, v in writable.items()
            if v.get("required") and k not in covered
            and v["type"] not in ("many2one", "one2many", "many2many"))
        return out

    # ---- deterministic per-row application ------------------------------

    @staticmethod
    def _cast(val, kind: str):
        if val is None:
            return None
        try:
            if kind == "int":
                return int(float(val))
            if kind == "float":
                return float(val)
            if kind == "bool":
                return str(val).lower() in ("1", "true", "yes", "y", "t", "present")
            if kind == "date":
                return str(val)[:10]
            if kind == "datetime":
                s = str(val)
                return s[:19] if len(s) >= 19 else f"{s[:10]} 00:00:00"
            return str(val)
        except (TypeError, ValueError):
            return None

    def apply(self, plan: dict, row: dict) -> tuple[dict, list[str]]:
        vals, warn = {}, []

        for col, spec in plan["fields"].items():
            if col not in row or row[col] is None:
                continue
            v = self._cast(row[col], spec.get("cast", "str"))
            mapping = spec.get("values") or {}
            if mapping:
                v = mapping.get(str(row[col]), mapping.get(str(v), v))
            if v is not None:
                vals[spec["target"]] = v

        for col, spec in plan["relations"].items():
            raw = row.get(col)
            if raw is None:
                continue
            oid = state.lookup_odoo_id(spec["entity"], raw)
            if oid:
                vals[spec["target"]] = oid
            else:
                warn.append(f"unresolved {col}={raw} -> {spec['entity']}")

        for tgt, tmpl in plan["computed"].items():
            try:
                s = tmpl.format(**{k: ("" if v is None else v) for k, v in row.items()})
                s = " ".join(s.split())
                if s:
                    vals[tgt] = s
            except (KeyError, IndexError):
                warn.append(f"computed {tgt} failed")

        for tgt, dv in plan["defaults"].items():
            vals.setdefault(tgt, dv)

        return vals, warn


# ═══════════════════════════════════════════════════════════════════ LOADER ══

class Loader:
    """The writer. Create, update, archive -- and nothing else."""

    def __init__(self, odoo: OdooRPC):
        self.odoo = odoo
        self._triage: dict[str, dict] = {}

    def _diagnose(self, model: str, err: str, vals: dict) -> dict:
        """Ask Gemini what to do about an Odoo rejection. One call per signature."""
        sig = f"{model}|{err[:120]}"
        if sig in self._triage:
            return self._triage[sig]
        prompt = f"""An Odoo XML-RPC write was rejected.


Model: {model}
Values sent: {json.dumps(vals, default=str)[:1500]}
Odoo error: {err[:1200]}

Choose the single best recovery:
- {{"action":"drop_field","field":"<field>"}}  the named field caused it; retry without it
- {{"action":"retry"}}                          transient; retry once unchanged
- {{"action":"skip"}}                           unrecoverable for this record

Return ONLY the JSON object."""
        try:
            d = ask_json(prompt, attempts=2)
        except LLMUnavailable:
            d = {"action": "skip"}
        if d.get("action") not in ("drop_field", "retry", "skip"):
            d = {"action": "skip"}
        self._triage[sig] = d
        return d

    def load(self, entity: str, model: str, records: list[dict]) -> dict:
        stats = {"created": 0, "updated": 0, "archived": 0, "deleted": 0,
                 "failed": 0, "errors": []}

        for rec in records:
            pid, op, vals = rec["pieas_id"], rec["op"], rec.get("vals", {})
            try:
                if op == "archive":
                    how = self.odoo.archive(model, rec["odoo_id"])
                    state.mark_archived(entity, pid)
                    stats[how] += 1
                elif op == "write":
                    self.odoo.write(model, rec["odoo_id"], vals)
                    state.upsert(entity, pid, model, rec["odoo_id"], rec["hash"])
                    stats["updated"] += 1
                else:
                    oid = self.odoo.create(model, vals)
                    state.upsert(entity, pid, model, oid, rec["hash"])
                    stats["created"] += 1
            except Exception as e:
                msg = str(e)
                # Never retry an archive through this path: it has no vals to
                # repair, and the retry branch below would fall through to
                # create() and produce a duplicate of the very record we were
                # trying to retire.
                d = {"action": "skip"} if op == "archive" \
                    else self._diagnose(model, msg, vals)
                retried = False
                if d["action"] == "drop_field" and d.get("field") in vals:
                    vals.pop(d["field"])
                    retried = True
                elif d["action"] == "retry":
                    retried = True
                if retried:
                    try:
                        if op == "write":
                            self.odoo.write(model, rec["odoo_id"], vals)
                            state.upsert(entity, pid, model, rec["odoo_id"], rec["hash"])
                            stats["updated"] += 1
                        else:
                            oid = self.odoo.create(model, vals)
                            state.upsert(entity, pid, model, oid, rec["hash"])
                            stats["created"] += 1
                        continue
                    except Exception as e2:
                        msg = str(e2)
                stats["failed"] += 1
                if len(stats["errors"]) < 5:
                    stats["errors"].append(f"{entity}#{pid}: {msg[:200]}")
        return stats
