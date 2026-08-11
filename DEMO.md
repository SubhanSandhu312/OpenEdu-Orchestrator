# Running a demo

Two ways to demo this. **Option 1 needs nothing installed** and works on any machine — use it if
you're presenting on a laptop that isn't this one, or if anything breaks on the day. **Option 2**
is the one that actually impresses, because your supervisor sees records appear in a real
OpenEduCat UI.

---

## Option 1 — Zero-setup demo (mock target)

No Odoo, no MySQL. Everything runs against local SQLite files.

### In VS Code

1. `File → Open Folder…` → `C:\Users\Shayan\projects\openedu`
2. Open a terminal: **Ctrl + `** (backtick)
3. Run:

```bash
.venv\Scripts\python -m openedu_orchestrator demo
```

That single command walks the entire design end to end and prints each stage:

| Step | What your supervisor sees |
|---|---|
| 1. Seed | 60 students, 18 faculty, 14 courses, ~84 marks generated |
| 2. Bulk migration | every record copied across, paginated |
| 3. Simulate activity | edits, new admissions, deletions on the source |
| 4. Change cycle | picks up **only** what changed |
| 5. Deletion cycle | archives what disappeared |
| 6. Re-run both | proves idempotency — nothing duplicates |
| 7. Status table | source rows vs target rows, side by side |

The final table is the money shot: **PIEAS rows == OpenEduCat active** for all four entity types.

### First-time setup (only if the venv is missing)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e .
```

---

## Option 2 — The real demo (live Odoo)

This shows records landing in an actual OpenEduCat web UI. Much more convincing.

### Step 1: Check the services are up

MySQL runs as a Windows service and starts automatically. Check both:

```bash
sc query PieasMySQL
curl -s -o nul -w "%{http_code}" http://localhost:8070
```

You want `STATE : 4 RUNNING` and `200`. If Odoo isn't responding, start it:

```bash
C:\Users\Shayan\projects\odoo-openeducat\venv\Scripts\python.exe C:\Users\Shayan\projects\odoo-openeducat\odoo\odoo-bin -c C:\Users\Shayan\projects\odoo-openeducat\odoo.conf
```

Leave that terminal running — it's the server. Give it ~20 seconds, then re-check the URL.
In VS Code, use a **second** terminal (the `+` icon in the terminal panel) for the demo commands.

### Step 2: Open OpenEduCat and log in

Browser → <http://localhost:8070> → log in with the local dev credentials (`admin` / `admin`).

Once logged in, these URLs jump straight to the data:

| What | URL |
|---|---|
| Students | <http://localhost:8070/odoo/action-257> |
| Faculty | <http://localhost:8070/odoo/action-263> |
| Subjects (PIEAS "courses") | <http://localhost:8070/odoo/action-262> |
| Exam marks | <http://localhost:8070/odoo/action-440> |

Have the Students tab open and visible before you start — that's the "before" picture.

### Step 3: The live sync

In your VS Code terminal:

```bash
.venv\Scripts\python -m openedu_orchestrator status --source pieas-mysql --target real
```

Shows current state on both sides. Then make a change to the source system, exactly as if
someone had edited it on the PIEAS website:

```bash
.venv\Scripts\python -m openedu_orchestrator mutate --source pieas-mysql --entity student --update 1 --insert 0 --delete 0
```

It prints which student ID it changed. Now run the sync:

```bash
.venv\Scripts\python -m openedu_orchestrator sync --source pieas-mysql --target real --entity student --once
```

You'll see `fetched=1 updated=1 errors=0 validation_issues=0`.

**Now refresh the Odoo Students tab** and find that student — the change is there. That moment
is the demo.

### Step 4: The cutover safety check

```bash
.venv\Scripts\python -m openedu_orchestrator reconcile --source pieas-mysql --target real
```

Read-only. Compares both systems and reports any mismatch. This is the "how do we know it's safe
to switch over?" answer.

---

## Showing the tests (a strong 30 seconds)

104 passing tests is a good visual.

**Terminal version:**
```bash
.venv\Scripts\python -m pytest -q
```

**VS Code Test Explorer version** (better for a demo — a wall of green ticks):
1. Click the **flask icon** in the left sidebar (Testing)
2. Click the refresh/reload icon if it's empty
3. Click **Run Tests** at the top

The repo now ships a `.vscode/settings.json` that points VS Code at the right interpreter and
enables pytest, so this should work with no configuration.

If tests don't appear: `Ctrl+Shift+P` → *Python: Select Interpreter* → pick the one under
`.\.venv\Scripts\python.exe`.

---

## Suggested 5-minute running order

1. **Frame the problem** (30s) — the source system has no API, so nothing can tell us when data
   changes. Everything follows from that.
2. **Show `demo`** (90s) — the whole lifecycle in one command, mock target. Point at step 6:
   re-running changes nothing, which is what makes it safe to schedule.
3. **Switch to real Odoo** (2m) — Students list open, `mutate`, `sync`, refresh, change is there.
4. **`reconcile`** (30s) — the pre-cutover safety check.
5. **Test Explorer** (30s) — 104 green.

If asked what's left: deployment target and secrets-vault choice are open decisions, not missing
code. See `report/SUMMARY.md` §7.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named openedu_orchestrator` | You're using the wrong Python. Prefix commands with `.venv\Scripts\python -m` |
| Odoo URL not loading | Server isn't running — see Step 1. Check `odoo_server.log` in the odoo-openeducat folder |
| `Odoo authentication failed` | Odoo is up but credentials differ; check `OPENEDU_ODOO_*` env vars, or `.env` if you made one |
| MySQL connection refused | `sc start PieasMySQL` (may need an admin terminal) |
| Sync says `already migrated` | Expected — bulk runs once. Use `sync`, not `migrate` |
| Real-target sync errors about duplicates | Local state and Odoo have drifted. Run `reconcile` to see it. For a clean demo, prefer Option 1 |
| Want a totally clean slate (mock) | `.venv\Scripts\python -m openedu_orchestrator seed` resets the source, mock target, and state store |

---

## Important: the mock demo and the real demo share one state store

If you run the mock `demo` and then try a real-target sync, you'll see:

```
Error: student: this state store was last synced to target 'mock', but you are
running against 'real'. ...
```

**This is a safety guard, not a bug.** The state store maps each source record to the target's
internal id. Those ids only mean something in *one* target. After the mock demo the store says
`PIEAS-STU-00001 → id 1`, but id 1 in the real Odoo is an unrelated OpenEduCat demo student — so
continuing would have silently overwritten that record with PIEAS data. The guard blocks it.

### Recovering for a real-target demo

```bash
del data\orchestrator_state.db
.venv\Scripts\python -m openedu_orchestrator rebuild-state --source pieas --target real
```

`rebuild-state` re-discovers what's already in Odoo using the external IDs the pipeline registered
when it first synced each record, and rebuilds the local mapping from that. It **only writes the
local state store — never the target.** Takes a couple of minutes (one lookup per record).

Then continue with Step 3 above.

### Recommended demo order

Do the **real demo first**, then the mock demo — the mock `demo` command resets the state store,
so running it last costs you nothing. If you must do mock first, budget the `rebuild-state` step.
