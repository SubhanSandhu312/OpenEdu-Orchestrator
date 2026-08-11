"""
The LangGraph state machine.

                    ┌──────────────────┐
        START ─────▶│   orchestrator   │────(entities exhausted)───▶ END
                    └────────┬─────────┘
                             │ dispatch
                             ▼
                        extractor ──▶ transformer ──▶ loader
                             ▲                          │
                             └──────────────────────────┘
                                  (back to orchestrator)

The Orchestrator is the conditional router and the only node that reads or writes
the state store. The three workers are stateless: they receive everything they
need on the state object and hand back what they produced.

The same graph runs all three phases -- bulk, incremental and deletions. Only the
plan the Orchestrator writes differs.
"""
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph, START, END

import config
import state as store
from agents import Orchestrator, Extractor, Transformer, Loader
from connectors import PieasDB, OdooRPC


class SyncState(TypedDict, total=False):
    run_phase: str            # bulk | incremental | deletions
    plan: list[dict]          # written once by the Orchestrator
    idx: int                  # cursor into plan
    current: dict | None      # the entity being worked

    rows: list[dict]          # Extractor output
    records: list[dict]       # Transformer output
    warnings: list[str]

    totals: Annotated[dict, lambda a, b: {**a, **b}]
    done: bool


def build(pieas: PieasDB, odoo: OdooRPC):
    orch = Orchestrator(pieas)
    extractor = Extractor(pieas)
    transformer = Transformer(pieas, odoo)
    loader = Loader(odoo)

    # ── the decision-maker ────────────────────────────────────────────────
    def n_orchestrator(s: SyncState) -> dict:
        plan, idx = s.get("plan"), s.get("idx", 0)

        if plan is None:
            print(f"\n[orchestrator] planning run: {s['run_phase']}")
            plan = orch.build_plan(s["run_phase"])
            for p in plan:
                print(f"   {p['entity']:<14} -> {p['phase']:<12} {p.get('reason','')[:60]}")
            idx = 0
        elif s.get("current"):
            # finalize the entity that just finished
            cur = s["current"]
            st = s.get("totals", {}).get(cur["entity"], {})
            orch.finalize(cur["entity"], cur["phase"], s.get("rows", []), st)

        if idx >= len(plan):
            return {"plan": plan, "idx": idx, "current": None, "done": True}

        cur = plan[idx]
        print(f"\n[orchestrator] {cur['entity']} ({cur['phase']}) -> {cur['odoo_model']}")
        print(f"   instruction: {cur['instruction']}")
        return {"plan": plan, "idx": idx + 1, "current": cur,
                "rows": [], "records": [], "done": False}

    # ── the pure fetcher ──────────────────────────────────────────────────
    def n_extractor(s: SyncState) -> dict:
        cur = s["current"]
        rows = extractor.fetch(cur["entity"], cur["phase"], cur["instruction"])
        print(f"   [extractor]   fetched {len(rows)} row(s)")
        return {"rows": rows}

    # ── the pure function ─────────────────────────────────────────────────
    def n_transformer(s: SyncState) -> dict:
        cur, rows = s["current"], s.get("rows", [])
        entity, model, phase = cur["entity"], cur["odoo_model"], cur["phase"]
        records, warnings = [], []

        if phase == "deletions":
            # Ghost detection is the Orchestrator's state comparison: what we
            # remember as live, minus what PIEAS just showed us.
            ghosts = orch.detect_ghosts(entity, [r["id"] for r in rows])
            for pid in ghosts:
                m = store.lookup(entity, pid)
                if m and m["status"] == "active":
                    records.append({"pieas_id": pid, "op": "archive",
                                    "odoo_id": m["odoo_id"], "hash": m["row_hash"]})
            print(f"   [transformer] {len(records)} ghost(s) to archive")
            return {"records": records, "warnings": warnings}

        try:
            plan = transformer.plan_for(entity, model)
        except Exception as e:
            # Better to sync nothing for this entity than to create records
            # that are missing required fields.
            print(f"   [transformer] cannot map {entity}: {str(e)[:200]}")
            print(f"   [transformer] skipping {entity} -- nothing written")
            return {"records": [], "warnings": [f"{entity}: {e}"]}

        if plan.get("unmet_required"):
            print(f"   [transformer] warning: unmapped required field(s): "
                  f"{plan['unmet_required']}")

        skipped = 0
        for row in rows:
            vals, warn = transformer.apply(plan, row)
            warnings.extend(warn)
            h = store.row_hash(vals)
            m = store.lookup(entity, row["id"])

            if m and m["row_hash"] == h and m["status"] == "active":
                skipped += 1           # genuinely unchanged, nothing to write
                continue
            if m and odoo.exists(model, m["odoo_id"]):
                if m["status"] == "archived":
                    vals["active"] = True     # it came back
                records.append({"pieas_id": row["id"], "op": "write",
                                "odoo_id": m["odoo_id"], "vals": vals, "hash": h})
            else:
                records.append({"pieas_id": row["id"], "op": "create",
                                "vals": vals, "hash": h})

        msg = f"   [transformer] {len(records)} record(s) mapped to {model}"
        if skipped:
            msg += f", {skipped} unchanged"
        print(msg)
        if warnings:
            print(f"   [transformer] {len(warnings)} warning(s), e.g. {warnings[0]}")
        return {"records": records, "warnings": warnings}

    # ── the writer ────────────────────────────────────────────────────────
    def n_loader(s: SyncState) -> dict:
        cur, records = s["current"], s.get("records", [])
        if not records:
            return {"totals": {cur["entity"]: {"created": 0, "updated": 0,
                                               "archived": 0, "deleted": 0,
                                               "failed": 0}}}
        st = loader.load(cur["entity"], cur["odoo_model"], records)
        print(f"   [loader]      created={st['created']} updated={st['updated']} "
              f"archived={st['archived']} deleted={st['deleted']} "
              f"failed={st['failed']}")
        for e in st["errors"]:
            print(f"                 ! {e}")
        return {"totals": {cur["entity"]: st}}

    g = StateGraph(SyncState)
    g.add_node("orchestrator", n_orchestrator)
    g.add_node("extractor", n_extractor)
    g.add_node("transformer", n_transformer)
    g.add_node("loader", n_loader)

    g.add_edge(START, "orchestrator")
    g.add_conditional_edges(
        "orchestrator",
        lambda s: END if s.get("done") else "extractor",
        {END: END, "extractor": "extractor"},
    )
    g.add_edge("extractor", "transformer")
    g.add_edge("transformer", "loader")
    g.add_edge("loader", "orchestrator")

    # One entity = one full lap. Ample headroom for the configured entity list.
    return g.compile()


def run(run_phase: str) -> dict:
    """Execute one complete pass of the pipeline."""
    store.init()
    pieas, odoo = PieasDB(), OdooRPC()
    app = build(pieas, odoo)
    final = app.invoke({"run_phase": run_phase, "idx": 0, "totals": {}},
                       {"recursion_limit": 4 * len(config.ENTITIES) + 20})

    totals = final.get("totals", {})
    agg = {k: sum(v.get(k, 0) for v in totals.values())
           for k in ("created", "updated", "archived", "deleted", "failed")}
    print(f"\n{'='*66}\n  {run_phase.upper()} complete  |  "
          f"created {agg['created']}  updated {agg['updated']}  "
          f"archived {agg['archived']}  deleted {agg['deleted']}  "
          f"failed {agg['failed']}\n{'='*66}")
    return agg
