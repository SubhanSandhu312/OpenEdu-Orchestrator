# OpenEdu Orchestrator — Simple Summary

*A plain-language version of the 24-page implementation report. Read this to understand and
present the project; read `OpenEdu_Orchestrator_Report_v2.tex` for the full technical detail.*

---

## 1. What is this project, in one line

**Software that copies PIEAS's student, faculty, course, and exam-mark data into OpenEduCat (the new
university system), and then keeps both sides matching automatically — forever.**

---

## 2. The problem

PIEAS wants to move to **OpenEduCat**, a free open-source university management system built on
Odoo. Two separate problems come with that:

1. **Move everything once.** All existing students, teachers, courses, and exam marks must be
   copied over.
2. **Keep it matching after that.** PIEAS keeps being used while the move happens. New students
   get admitted, records get corrected, some get removed. OpenEduCat has to reflect all of it.

**The hard part:** PIEAS has no API. It cannot notify us when something changes. There is no
"ping" when a student is added.

So the system cannot wait to be told. It has to **go and check, on a schedule** — like a
security guard doing rounds, not a doorbell that rings.

Everything in the design comes from that one limitation.

---

## 3. How it works — four workers with four jobs

Think of it as a small team where **nobody does anybody else's job**:

| Worker | Its one job | Simple analogy |
|---|---|---|
| **Extractor** | Reads data out of PIEAS. Nothing else. | The person who fetches files from the old cabinet |
| **Transformer** | Renames/reshapes fields so OpenEduCat understands them. | The translator |
| **Loader** | Writes the data into OpenEduCat. | The person who files it in the new cabinet |
| **Orchestrator** | **Decides everything** — what to fetch, and whether each record is new, changed, or unchanged. | The manager |
| **Validator** *(optional)* | Reads the record back afterwards to confirm it saved correctly. | The quality checker |

Only the **Orchestrator** makes decisions. The other three just do exactly what they're told.
That is deliberate — it means if something goes wrong, there is exactly one place to look.

### How it detects changes

Every PIEAS record has a "last updated" time. The system remembers the time of its last
successful run, then next time asks: *"give me everything updated after that time."*

### How it detects deletions

This needs a **separate check**, and the reason is important:

> If a record is deleted, it's gone. A query asking "what changed?" can never return it —
> there's nothing left to return.

So separately (less often), the system asks PIEAS for the **full list of IDs that still exist**,
and compares it against its own records. Anything in its list but missing from PIEAS's list was
deleted.

### What happens to a deleted record

It is **archived, not deleted** — marked inactive but kept. If someone deleted a student by
mistake, or it was actually just a name change, nothing is lost.

---

## 4. What is actually working right now

This is the important part for a presentation — **this is not a design document or a prototype.
It is running software.**

| Thing | Status |
|---|---|
| The 4 agents + validator | Built and working |
| Copying all data over (bulk migration) | Working, tested on a real system |
| Detecting new + changed records | Working, tested on a real system |
| Detecting deleted records | Working, tested on a real system |
| Running against a **real Odoo + OpenEduCat installation** | Working |
| Running against a **real MySQL database** as the PIEAS stand-in | Working |
| Supporting **other universities**, not just PIEAS | Working (3 different sources supported) |
| Syncing **exam marks** (the report's own example) | Working, tested on a real system |
| Auto-creating supporting records (department, batch, enrolment, exam) | Working |
| Automated tests | **104 tests, all passing** |
| Tests run automatically on every code change | Working (GitHub CI) |

### The numbers

- **104 automated tests** — all passing (was 57 in the first version)
- **89 records** successfully verified in the real OpenEduCat system
- **4** entity types synced: students, faculty, courses, exam marks
- **3** different data sources supported
- **2** targets supported (a fake one for testing, and the real Odoo system)
- **5** real bugs found and fixed — see below

---

## 5. The strongest thing to tell your supervisor

Earlier, everything was tested against a **fake/simulated** OpenEduCat. All 57 tests passed.
It looked complete.

Then we connected it to a **real Odoo + OpenEduCat installation** — and immediately found
**5 genuine bugs** that the fake version could never have revealed:

1. Updating a record failed on the real system (worked fine on the fake one).
2. A function was passing its arguments in the wrong shape to the real Odoo API.
3. Counting records crashed — because of a broken piece of code inside OpenEduCat itself.
4. The quality-checker was reporting false alarms on every single write.
5. The logging system crashed due to a naming conflict with Python's own library.

**Why this matters:** it proves the value of testing against real infrastructure instead of
assumptions. A passing test suite is not the same as a working system. Every one of those bugs
would have appeared on the first day of real use.

Importantly: **fixing all five required no change to the four agents' design.** That is strong
evidence the original architecture was correct.

---

## 6. Extra things built beyond the original plan

- **Works for any university, not just PIEAS.** Originally PIEAS was hardcoded everywhere. Now
  there is a proper plug-in system — adding a new university means writing one small adapter
  file, not editing the main system.
- **An AI helper for setting up field mappings.** When onboarding a new university, someone must
  work out "their `student_no` is our `gr_no`" across dozens of fields. An LLM (Gemini) now
  reads both systems' structures and drafts that mapping for a human to review.
  - **Deliberately NOT used during actual syncing.** It only helps a human once during setup.
    Data migration must be exact and repeatable, so no AI guessing per record.
- **A safety-check command (`reconcile`).** Before finally switching over to OpenEduCat, this
  compares both systems and reports anything that doesn't match. It only reads, never changes
  anything.
- **Reliability features:** automatic retry if the network drops, proper logging for monitoring,
  and passwords moved out of the code into environment settings.
- **Exam marks, and the supporting records they need.** A mark is different from a student: it
  only makes sense *attached to* a student and a subject. So the system now understands that
  some records depend on others, and syncs them in the right order. It also auto-creates the
  supporting records OpenEduCat needs but PIEAS doesn't have — departments, batches, enrolments,
  and exams — reusing them instead of duplicating (all Physics 2027 students share one batch).
  If a mark arrives for a student who hasn't been synced yet, it stops with a clear error
  instead of writing a broken link.

---

## 7. What's left

**Two decisions needed (not coding problems — they need a decision from above):**

1. **Where will this run?** A server, a cloud service, or scheduled functions. This affects how
   it gets scheduled and monitored.
2. **How should passwords be stored in production?** Currently they load from environment
   settings, which is correct and standard. Connecting to a specific password vault depends on
   answer #1.

**Next development step:** adding a larger AI/LLM layer on top. The current system was
deliberately built so this can be *added on top* rather than requiring a rewrite — the mapping
helper described above is the first example of that pattern.

---

## 8. Likely questions, and short answers

**"Is this using AI?"**
The syncing engine itself is deliberately *not* AI-based — it is exact and repeatable, which is
what data migration requires. It *is* "agentic" in the multi-agent-systems sense: independent
cooperating components with one decision-maker. Separately, an LLM is used as a setup-time
helper tool. Both are real, they are just different meanings of the word.

**"Why not just write one script?"**
Because the four jobs change for different reasons. Connecting a different university only
changes the Extractor. Changing the target system only changes the Loader. When they were
separated, adding real Odoo support required *zero* changes to the other three.

**"Is it tested?"**
104 automated tests, running automatically on every change, on two Python versions. Plus manual
verification against the real Odoo system, confirmed by reading the data back independently
rather than trusting the program's own report.

**"What if it crashes halfway through the migration?"**
It resumes safely. It records what it has already synced, so re-running it does not duplicate
anything — it picks up where it stopped.

**"How do we know the migration is complete before switching over?"**
The `reconcile` command. It compares both systems and reports any mismatch, and exits with an
error code if anything is wrong — so it can be used as an automatic gate before going live.
