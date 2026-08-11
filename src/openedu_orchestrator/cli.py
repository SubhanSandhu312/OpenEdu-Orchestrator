"""Command-line entry point for exercising the sync pipeline end to end.

This is the operator-facing surface for the test build: seed dummy PIEAS
data, run bulk migration, simulate ongoing PIEAS activity, run the change and
deletion cycles, and inspect the resulting state. None of this logic belongs
to any agent -- it just wires the agents together the way a scheduler
(cron, Celery beat, etc.) would in production.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from openedu_orchestrator import sync_store
from openedu_orchestrator.agents.extractor import ExtractorAgent
from openedu_orchestrator.agents.loader import LoaderAgent
from openedu_orchestrator.agents.orchestrator import OrchestratorAgent
from openedu_orchestrator.agents.validator import ValidationAgent
from openedu_orchestrator.agents.transformer import TransformerAgent
from openedu_orchestrator.config import (
    CHANGE_CYCLE_INTERVAL_SECONDS,
    ENTITY_TYPES,
    OPENEDUCAT_DB_PATH,
    OPENEDUCAT_MODEL_FOR_ENTITY,
    PIEAS_DB_PATH,
    PIEAS_TABLE_FOR_ENTITY,
    SYNC_STORE_DB_PATH,
)
from openedu_orchestrator.models import PieasStudent
from openedu_orchestrator.openeducat_client import OpenEduCatClient, OdooXmlRpcClient
from openedu_orchestrator.openeducat_client import reset_database as reset_oc_db
from openedu_orchestrator import pieas_source as src
from openedu_orchestrator import pieas_source_mysql
from openedu_orchestrator.pieas_source import reset_database as reset_pieas_db
from openedu_orchestrator.graph import run_cycle
from openedu_orchestrator.logging_config import configure_logging
from openedu_orchestrator.seed import seed_pieas
from openedu_orchestrator import mapping_authoring as ma

console = Console()
configure_logging()

# --source: which physical store backs PIEAS. Both represent the same
# *logical* source system ("pieas"), just different backing technology --
# see source_registry.py / sync_store's source_system scoping, which
# distinguishes logical sources (PIEAS vs. a different university), not
# physical database engines.
SOURCE_MODULES = {"pieas": src, "pieas-mysql": pieas_source_mysql}

# Real op.course actually represents a degree program, not a subject --
# course maps to op.subject on the real target, op.course on the mock
# (see docs/mapping_authoring_tool.md and the course-rework commit).
REAL_MODEL_FOR_ENTITY = {**OPENEDUCAT_MODEL_FOR_ENTITY, "course": "op.subject"}


def _entity_list(entity: str) -> list[str]:
    return list(ENTITY_TYPES) if entity == "all" else [entity]


def _build_agents(source: str = "pieas", target: str = "mock"):
    source_module = SOURCE_MODULES[source]
    db_path = PIEAS_DB_PATH if source == "pieas" else None
    extractor = ExtractorAgent(db_path, source=source_module)
    # source_system stays "pieas" regardless of which physical store backs
    # it -- SQLite and MySQL are two backing stores for the same logical
    # PIEAS system in this build, not two different source systems.
    orchestrator = OrchestratorAgent(SYNC_STORE_DB_PATH, source_system="pieas", target=target)
    if target == "real":
        client = OdooXmlRpcClient()
        loader = LoaderAgent(client=client, model_for_entity=REAL_MODEL_FOR_ENTITY)
    else:
        client = OpenEduCatClient(OPENEDUCAT_DB_PATH)
        loader = LoaderAgent(client)
    validator = ValidationAgent(loader)
    return orchestrator, extractor, loader, validator


def _transform_fn_for(entity_type: str, target: str):
    """TransformerAgent.transform (graph.py's own default) for the mock;
    the approved, human-reviewed compiled mapping for the real target --
    see mappings/*.json and docs/mapping_authoring_tool.md.
    """
    if target == "mock":
        return TransformerAgent.transform
    mapping_path = Path(f"mappings/{entity_type}_pieas.json")
    if not mapping_path.exists():
        raise click.ClickException(
            f"No approved real-target mapping at {mapping_path} for entity_type={entity_type!r}. "
            f"Run the mapping-authoring tool and get it reviewed before syncing this entity to a real target."
        )
    return ma.compile_mapping(ma.load_mapping(mapping_path))


_SOURCE_OPTION = click.option(
    "--source", type=click.Choice(list(SOURCE_MODULES)), default="pieas", show_default=True,
    help="Which physical store backs PIEAS.",
)
_TARGET_OPTION = click.option(
    "--target", type=click.Choice(["mock", "real"]), default="mock", show_default=True,
    help="mock = local SQLite test double; real = the live Odoo/OpenEduCat instance.",
)


class _Cli(click.Group):
    """Renders TargetMismatchError as a clean operator-facing message rather
    than a stack trace. It is a guard doing its job, not a crash, and the
    message already says exactly what to do about it.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except sync_store.TargetMismatchError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=_Cli)
def cli():
    """OpenEdu Orchestrator -- agentic PIEAS -> OpenEduCat sync (test build)."""


@cli.command()
@_SOURCE_OPTION
@click.option("--students", default=60, show_default=True)
@click.option("--faculty", default=18, show_default=True)
@click.option("--courses", default=14, show_default=True)
@click.option("--seed", default=42, show_default=True)
@click.option("--full-reset/--pieas-only", default=True, help="Also wipe OpenEduCat + sync store.")
def seed(source: str, students: int, faculty: int, courses: int, seed: int, full_reset: bool):
    """(Re)create the dummy PIEAS database with fake data."""
    source_module = SOURCE_MODULES[source]
    conn_info = PIEAS_DB_PATH if source == "pieas" else None
    conn = source_module.reset_database(conn_info)
    # Keyword args deliberately: this was positional, and adding a parameter
    # to seed_pieas silently shifted `seed` into it -- producing ~1200 marks
    # instead of ~90 with no error anywhere.
    counts = seed_pieas(
        conn, num_students=students, num_faculty=faculty, num_courses=courses,
        seed=seed, source=source_module,
    )
    conn.close()
    console.print(f"[green]Seeded PIEAS ({source})[/green]: {counts}")
    if full_reset:
        reset_oc_db(OPENEDUCAT_DB_PATH).close()
        sync_store.reset_database(SYNC_STORE_DB_PATH)
        console.print("[green]Reset OpenEduCat mock + orchestrator state store[/green]")


@cli.command()
@_SOURCE_OPTION
@_TARGET_OPTION
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
def migrate(source: str, target: str, entity: str):
    """Run the one-time bulk migration cycle."""
    orchestrator, extractor, loader, validator = _build_agents(source, target)
    for et in _entity_list(entity):
        if not orchestrator.is_bulk_mode(et):
            console.print(f"[yellow]{et}: already migrated (sync_mapping is non-empty) -- skipping bulk, "
                          f"run 'sync' instead[/yellow]")
            continue
        report = run_cycle("bulk", et, orchestrator, extractor, loader, validator,
                            transform_fn=_transform_fn_for(et, target))
        _print_report("BULK MIGRATION", report)
    extractor.close(); loader.close(); orchestrator.close()


@cli.command()
@_SOURCE_OPTION
@_TARGET_OPTION
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
@click.option("--loop/--once", default=False, help="Keep running on an interval instead of a single pass.")
@click.option("--interval", default=CHANGE_CYCLE_INTERVAL_SECONDS, show_default=True, help="Seconds between loop passes.")
def sync(source: str, target: str, entity: str, loop: bool, interval: int):
    """Run the change-detection cycle (timestamp watermark)."""
    orchestrator, extractor, loader, validator = _build_agents(source, target)
    try:
        while True:
            for et in _entity_list(entity):
                report = run_cycle("change", et, orchestrator, extractor, loader, validator,
                                    transform_fn=_transform_fn_for(et, target))
                _print_report("CHANGE CYCLE", report)
            if not loop:
                break
            console.print(f"[dim]sleeping {interval}s...[/dim]")
            time.sleep(interval)
    finally:
        extractor.close(); loader.close(); orchestrator.close()


@cli.command(name="deletion-check")
@_SOURCE_OPTION
@_TARGET_OPTION
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
def deletion_check(source: str, target: str, entity: str):
    """Run the deletion-detection cycle (full ID list vs. sync_mapping)."""
    orchestrator, extractor, loader, validator = _build_agents(source, target)
    for et in _entity_list(entity):
        # deletion mode skips the Transformer entirely (graph.py's own
        # routing) so transform_fn is irrelevant here regardless of target.
        report = run_cycle("deletion", et, orchestrator, extractor, loader, validator)
        _print_report("DELETION CYCLE", report)
    extractor.close(); loader.close(); orchestrator.close()


@cli.command()
@_SOURCE_OPTION
@click.option("--entity", type=click.Choice(["student", "faculty", "course", "mark"]), default="student")
@click.option("--update", "n_update", default=3, show_default=True, help="Rows to edit.")
@click.option("--insert", "n_insert", default=2, show_default=True, help="New rows to add.")
@click.option("--delete", "n_delete", default=1, show_default=True, help="Rows to remove.")
@click.option("--seed", default=None, type=int, help="RNG seed for reproducible mutation.")
def mutate(source: str, entity: str, n_update: int, n_insert: int, n_delete: int, seed: int | None):
    """Simulate PIEAS 'still being used': edits, new admissions, and removals."""
    source_module = SOURCE_MODULES[source]
    rng = random.Random(seed)
    conn_info = PIEAS_DB_PATH if source == "pieas" else None
    conn = source_module.get_connection(conn_info)
    table = PIEAS_TABLE_FOR_ENTITY[entity]
    ids = source_module.fetch_ids(conn, table)
    if not ids:
        console.print("[red]No rows to mutate -- run 'seed' first.[/red]")
        return

    updated, deleted = [], []
    for source_id in rng.sample(ids, k=min(n_update, len(ids))):
        if entity == "student":
            source_module.update_fields(conn, table, source_id, {"department": rng.choice(
                ["Computer Science", "Electrical Engineering", "Physics"])})
        elif entity == "faculty":
            source_module.update_fields(conn, table, source_id, {"designation": rng.choice(
                ["Lecturer", "Assistant Professor", "Associate Professor", "Professor"])})
        elif entity == "mark":
            # The report's own worked example of an ongoing change:
            # "a quiz mark is updated" (Section 2).
            source_module.update_fields(conn, table, source_id, {"marks_obtained": rng.randint(0, 10)})
        else:
            source_module.update_fields(conn, table, source_id, {"credit_hours": rng.choice([2, 3, 4])})
        updated.append(source_id)

    remaining = [i for i in ids if i not in updated]
    for source_id in rng.sample(remaining, k=min(n_delete, len(remaining))):
        source_module.delete_row(conn, table, source_id)
        deleted.append(source_id)

    inserted = []
    if entity == "student":
        existing = source_module.count_rows(conn, "students") + 100000
        for i in range(n_insert):
            new_id = f"PIEAS-STU-{existing + i:06d}"
            source_module.insert_student(conn, PieasStudent(
                pieas_id=new_id, roll_number=f"2026-CS-{existing + i}",
                first_name="New", last_name=f"Admit{i}", email=f"new.admit{i}.{existing}@example.com",
                gender=rng.choice(["male", "female"]), date_of_birth="2005-01-01",
                department="Computer Science", batch_year=2026,
                last_updated=datetime.now(timezone.utc),
            ))
            inserted.append(new_id)

    console.print(f"[cyan]{entity}[/cyan] ({source}): updated={updated} deleted={deleted} inserted={inserted}")
    conn.close()


@cli.command()
@_SOURCE_OPTION
@_TARGET_OPTION
def status(source: str, target: str):
    """Print current row counts and watermarks across all three databases."""
    source_module = SOURCE_MODULES[source]
    orchestrator, extractor, loader, validator = _build_agents(source, target)
    conn_info = PIEAS_DB_PATH if source == "pieas" else None
    pieas_conn = source_module.get_connection(conn_info)
    oc_client = loader._client
    model_for_entity = REAL_MODEL_FOR_ENTITY if target == "real" else OPENEDUCAT_MODEL_FOR_ENTITY

    if target == "real":
        console.print("[dim]Note: OpenEduCat active/archived counts include any pre-existing target "
                       "data (e.g. OpenEduCat's own demo records) -- 'sync_mapping rows' is the "
                       "accurate count of what this pipeline itself has actually synced.[/dim]")
    table = Table(title=f"OpenEdu Orchestrator -- status (source={source}, target={target})")
    table.add_column("Entity")
    table.add_column("PIEAS rows", justify="right")
    table.add_column("OpenEduCat active", justify="right")
    table.add_column("OpenEduCat archived", justify="right")
    table.add_column("sync_mapping rows", justify="right")
    table.add_column("watermark")

    for et in ENTITY_TYPES:
        pieas_table = PIEAS_TABLE_FOR_ENTITY[et]
        model = model_for_entity[et]
        pieas_count = source_module.count_rows(pieas_conn, pieas_table)
        # Not every real model has an `active` field (op.exam.attendees does
        # not), and filtering on one that doesn't exist is a hard error --
        # so ask before counting rather than crashing the whole table.
        if oc_client.supports_active(model):
            active_count = str(oc_client.search_count(model, [("active", "=", True)]))
            archived_count = str(oc_client.search_count(model, [("active", "=", False)]))
        else:
            active_count, archived_count = str(oc_client.search_count(model)), "n/a"
        mapping_count = orchestrator.mapping_count(et)
        watermark = orchestrator.get_watermark(et)
        table.add_row(
            et, str(pieas_count), str(active_count), str(archived_count),
            str(mapping_count), watermark.isoformat() if watermark else "-",
        )
    console.print(table)
    pieas_conn.close(); extractor.close(); loader.close(); orchestrator.close()


@cli.command()
@_SOURCE_OPTION
@_TARGET_OPTION
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
def reconcile(source: str, target: str, entity: str):
    """Read-only drift audit -- a pre-cutover sanity check.

    Compares the PIEAS source against sync_mapping and the target *without
    writing anything*, and flags gaps a sync/deletion cycle should already
    have closed (a stalled/interrupted cycle, or a crash mid-write). This is
    "verify", not "fix" -- re-running 'sync'/'deletion-check' is what closes
    any drift this reports. Exits 1 if any drift is found, 0 if clean, so it
    can gate a cutover in a script/CI job.

    Caveat: sync_mapping's openeducat_id is scoped by source_system, not by
    target -- there is exactly one live target per deployment in this
    design (see OrchestratorAgent / sync_store.py). Running reconcile with
    a different --target than what actually wrote the current mapping rows
    (e.g. switching mock<->real mid-session, as this test build lets you do)
    will report every mapped row as missing_target, since the stored id is
    from the other target's id space -- that's a mismatched invocation, not
    real drift.
    """
    source_module = SOURCE_MODULES[source]
    conn_info = PIEAS_DB_PATH if source == "pieas" else None
    pieas_conn = source_module.get_connection(conn_info)
    orchestrator, extractor, loader, validator = _build_agents(source, target)

    any_drift = False
    for et in _entity_list(entity):
        table_name = PIEAS_TABLE_FOR_ENTITY[et]
        source_ids = set(source_module.fetch_ids(pieas_conn, table_name))
        mappings = {m["source_id"]: m for m in orchestrator.all_mappings(et)}

        # In sync_mapping but never fetched from the source at all -- a bulk
        # migration or change cycle should have created this row and didn't.
        not_synced = sorted(source_ids - mappings.keys())

        missing_target, stale_archived, should_be_archived = [], [], []
        for source_id, mapping in mappings.items():
            actual = loader.read_back(et, mapping["openeducat_id"])
            if actual is None:
                # sync_mapping points at a target record that no longer
                # exists -- e.g. it was deleted directly in OpenEduCat,
                # outside this pipeline.
                missing_target.append(source_id)
                continue
            is_active = actual.get("active") not in (0, False)
            if source_id in source_ids and not is_active:
                # Still present at the source but archived on the target --
                # a deletion cycle ran against a source_id that has since
                # come back, or archived the wrong record.
                stale_archived.append(source_id)
            elif source_id not in source_ids and is_active:
                # No longer present at the source but still active on the
                # target -- the deletion cycle that should have archived
                # this hasn't run (or didn't get this far before failing).
                should_be_archived.append(source_id)

        drift = bool(not_synced or missing_target or stale_archived or should_be_archived)
        any_drift = any_drift or drift

        status_label = "[red]DRIFT[/red]" if drift else "[green]clean[/green]"
        console.print(f"[bold]{et}[/bold] ({source} -> {target}): {status_label}  "
                       f"source={len(source_ids)} mapped={len(mappings)}")
        if not_synced:
            console.print(f"  [red]not_synced[/red] ({len(not_synced)}): {not_synced}")
        if missing_target:
            console.print(f"  [red]missing_target[/red] ({len(missing_target)}): {missing_target}")
        if stale_archived:
            console.print(f"  [red]stale_archived[/red] ({len(stale_archived)}): {stale_archived}")
        if should_be_archived:
            console.print(f"  [red]should_be_archived[/red] ({len(should_be_archived)}): {should_be_archived}")

    pieas_conn.close(); extractor.close(); loader.close(); orchestrator.close()
    if any_drift:
        raise SystemExit(1)


@cli.command(name="rebuild-state")
@_SOURCE_OPTION
@_TARGET_OPTION
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
def rebuild_state(source: str, target: str, entity: str):
    """Rebuild the local state store from the target's own external IDs.

    Only writes the local state store -- never the target. Use it when
    sync_mapping has been lost or repointed (e.g. after running the mock
    'demo', which resets it) but records are already present in the real
    target. Because the pipeline registers every record's source id through
    Odoo's own ir.model.data, those records stay discoverable without the
    local store, so they are matched rather than duplicated.
    """
    source_module = SOURCE_MODULES[source]
    conn_info = PIEAS_DB_PATH if source == "pieas" else None
    pieas_conn = source_module.get_connection(conn_info)
    orchestrator, extractor, loader, validator = _build_agents(source, target)
    client = loader._client
    if not hasattr(client, "find_by_external_id"):
        raise click.ClickException(
            f"--target {target} has no external-ID lookup, so there is nothing to rebuild "
            f"from. This command is for recovering real-target state; for the mock, run "
            f"'seed' to start clean."
        )
    model_for_entity = REAL_MODEL_FOR_ENTITY if target == "real" else OPENEDUCAT_MODEL_FOR_ENTITY

    for et in _entity_list(entity):
        table_name = PIEAS_TABLE_FOR_ENTITY[et]
        model = model_for_entity[et]
        found = missing = 0
        for source_id in source_module.fetch_ids(pieas_conn, table_name):
            target_id = client.find_by_external_id(model, source_id)
            if target_id is None:
                missing += 1
                continue
            orchestrator.adopt_mapping(et, source_id, target_id, source_module.row_by_id(pieas_conn, table_name, source_id))
            found += 1
        orchestrator.claim_target(et)
        console.print(f"[green]{et}[/green]: adopted {found} existing mapping(s); "
                       f"{missing} source row(s) not yet in the target "
                       f"(a normal 'migrate'/'sync' will create those).")

    pieas_conn.close(); extractor.close(); loader.close(); orchestrator.close()


@cli.command()
def demo():
    """End-to-end walkthrough: seed -> bulk migrate -> mutate PIEAS -> change
    cycle -> deletion cycle -> status. Intended as the one command that shows
    the whole report's design working end to end.
    """
    ctx = click.get_current_context()
    console.rule("[bold]1. Seed dummy PIEAS data[/bold]")
    ctx.invoke(seed, students=60, faculty=18, courses=14, seed=42, full_reset=True)

    console.rule("[bold]2. Bulk migration (state store is empty)[/bold]")
    ctx.invoke(migrate, entity="all")

    console.rule("[bold]3. Simulate PIEAS still being used[/bold]")
    ctx.invoke(mutate, entity="student", n_update=4, n_insert=2, n_delete=2, seed=7)
    ctx.invoke(mutate, entity="faculty", n_update=2, n_insert=1, n_delete=1, seed=7)
    ctx.invoke(mutate, entity="course", n_update=2, n_insert=0, n_delete=1, seed=7)

    console.rule("[bold]4. Change cycle (timestamp watermark)[/bold]")
    ctx.invoke(sync, entity="all", loop=False, interval=0)

    console.rule("[bold]5. Deletion cycle (full ID list vs. sync_mapping)[/bold]")
    ctx.invoke(deletion_check, entity="all")

    console.rule("[bold]6. Re-run both cycles to demonstrate idempotency[/bold]")
    ctx.invoke(sync, entity="all", loop=False, interval=0)
    ctx.invoke(deletion_check, entity="all")

    console.rule("[bold]7. Final status[/bold]")
    ctx.invoke(status)


def _print_report(title: str, report) -> None:
    console.print(
        f"[bold]{title}[/bold] [{report.entity_type}] "
        f"fetched={report.fetched} created={report.created} updated={report.updated} "
        f"unchanged={report.unchanged} archived={report.archived} pages={report.pages} "
        f"errors={len(report.errors)} validation_issues={len(report.validation_issues)}"
    )
    for err in report.errors:
        console.print(f"  [red]error:[/red] {err}")
    for issue in report.validation_issues:
        console.print(f"  [yellow]validation:[/yellow] {issue}")


if __name__ == "__main__":
    cli()
