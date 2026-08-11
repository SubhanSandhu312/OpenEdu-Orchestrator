"""
Entry point.

    python run.py seed          create the PIEAS MySQL database + dummy data
    python run.py bulk          Phase 1 -- migrate everything
    python run.py incremental   one fast pulse  (catch inserts/updates)
    python run.py deletions     one slow pulse  (catch deletions -> archive)
    python run.py schedule      run both pulses forever, per config.py
    python run.py status        what is synced, and where the watermarks are
    python run.py check         verify MySQL / Odoo / Gemini connectivity
    python run.py reset         remove everything this pipeline wrote to Odoo

Change the cadence at the top of config.py.
"""
import os
import sys

import config


def cmd_seed():
    from connectors import PieasDB
    print(f"Seeding MySQL from {config.SEED_SQL} ...")
    PieasDB().run_script(config.SEED_SQL)
    db = PieasDB()
    print(f"Done. Database '{config.MYSQL['database']}':")
    for table, _ in config.ENTITIES:
        n = db.query(f"SELECT COUNT(*) n FROM `{table}`")[0]["n"]
        print(f"   {table:<14} {n:>4} rows")


def cmd_status():
    import state
    state.init()
    rows = state.status_report()
    print(f"\n{'entity':<14}{'odoo model':<22}{'active':>7}{'archived':>10}   watermark")
    print("-" * 86)
    for r in rows:
        print(f"{r['entity']:<14}{r['model']:<22}{r['active']:>7}{r['archived']:>10}   "
              f"{r['watermark']}")
    last = [r["last_deletion_scan"] for r in rows if r["last_deletion_scan"]]
    print(f"\nlast deletion scan: {max(last) if last else 'never'}")


def cmd_reset():
    """Undo every write this pipeline made, so the next run is a clean bulk.

    Only touches records listed in sync_mapping -- OpenEduCat's own bundled
    records are left alone.
    """
    import shutil
    import sqlite3
    import state
    from connectors import OdooRPC

    if not os.path.exists(config.STATE_DB):
        print("Nothing to reset (no state store).")
        return
    odoo = OdooRPC()
    cn = sqlite3.connect(config.STATE_DB)

    # Reverse dependency order: children before the parents they point at.
    for table, model in reversed(config.ENTITIES):
        ids = [r[0] for r in cn.execute(
            "SELECT odoo_id FROM sync_mapping WHERE entity=?", (table,))]
        if not ids:
            continue
        try:
            odoo.execute(model, "unlink", ids)
            print(f"   removed {len(ids):>3} {model}")
        except Exception as e:
            print(f"   ! {model}: {str(e)[:120]}")
    cn.close()

    os.remove(config.STATE_DB)
    shutil.rmtree(config.PLAN_CACHE, ignore_errors=True)
    print("State store and cached plans cleared. Next run will be a full bulk.")


def cmd_check():
    ok = True

    print("MySQL  ... ", end="")
    try:
        from connectors import PieasDB
        n = PieasDB().query("SELECT COUNT(*) n FROM students")[0]["n"]
        print(f"OK ({n} students in {config.MYSQL['database']})")
    except Exception as e:
        ok = False
        print(f"FAIL  {e}")

    print("Odoo   ... ", end="")
    try:
        from connectors import OdooRPC
        o = OdooRPC()
        mods = [m for m in ("openeducat_core", "openeducat_exam") if o.installed(m)]
        print(f"OK (uid={o.uid}, db={o.db}, installed: {', '.join(mods) or 'NONE'})")
        if len(mods) < 2:
            ok = False
            print("         ! openeducat_core and openeducat_exam must both be installed")
    except Exception as e:
        ok = False
        print(f"FAIL  {e}")

    print("Gemini ... ", end="")
    try:
        from agents import llm
        llm().invoke("Reply with the single word: ready")
        print(f"OK ({config.GEMINI_MODEL})")
    except Exception as e:
        ok = False
        print(f"FAIL  {e}")

    print("\nAll systems go." if ok else "\nFix the failures above before syncing.")
    return 0 if ok else 1


def cmd_schedule():
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    import graph

    sched = BlockingScheduler()

    sched.add_job(lambda: graph.run("incremental"), "interval",
                  minutes=config.INCREMENTAL_EVERY_MINUTES,
                  id="fast_pulse", max_instances=1, coalesce=True)

    sched.add_job(lambda: graph.run("deletions"),
                  CronTrigger(hour=config.DELETION_SCAN_HOUR,
                              minute=config.DELETION_SCAN_MINUTE),
                  id="slow_pulse", max_instances=1, coalesce=True)

    print("=" * 66)
    print("  SCHEDULER RUNNING          (Ctrl+C to stop)")
    print(f"  fast pulse  every {config.INCREMENTAL_EVERY_MINUTES} min   -> updates")
    print(f"  slow pulse  daily {config.DELETION_SCAN_HOUR:02d}:"
          f"{config.DELETION_SCAN_MINUTE:02d}     -> deletions")
    print("=" * 66)

    graph.run("incremental")          # don't idle until the first tick
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "seed":
        cmd_seed()
    elif cmd in ("bulk", "incremental", "deletions"):
        import graph
        graph.run(cmd)
    elif cmd == "schedule":
        cmd_schedule()
    elif cmd == "status":
        cmd_status()
    elif cmd == "check":
        return cmd_check()
    elif cmd == "reset":
        cmd_reset()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
