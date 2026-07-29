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

import click
from rich.console import Console
from rich.table import Table

from openedu_orchestrator import sync_store
from openedu_orchestrator.agents.extractor import ExtractorAgent
from openedu_orchestrator.agents.loader import LoaderAgent
from openedu_orchestrator.agents.orchestrator import OrchestratorAgent
from openedu_orchestrator.agents.validator import ValidationAgent
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
from openedu_orchestrator.openeducat_client import OpenEduCatClient
from openedu_orchestrator.openeducat_client import reset_database as reset_oc_db
from openedu_orchestrator import pieas_source as src
from openedu_orchestrator.pieas_source import reset_database as reset_pieas_db
from openedu_orchestrator.graph import run_cycle
from openedu_orchestrator.seed import seed_pieas

console = Console()


def _entity_list(entity: str) -> list[str]:
    return list(ENTITY_TYPES) if entity == "all" else [entity]


def _build_agents():
    orchestrator = OrchestratorAgent(SYNC_STORE_DB_PATH)
    extractor = ExtractorAgent(PIEAS_DB_PATH)
    client = OpenEduCatClient(OPENEDUCAT_DB_PATH)
    loader = LoaderAgent(client)
    validator = ValidationAgent(loader)
    return orchestrator, extractor, loader, validator


@click.group()
def cli():
    """OpenEdu Orchestrator -- agentic PIEAS -> OpenEduCat sync (test build)."""


@cli.command()
@click.option("--students", default=60, show_default=True)
@click.option("--faculty", default=18, show_default=True)
@click.option("--courses", default=14, show_default=True)
@click.option("--seed", default=42, show_default=True)
@click.option("--full-reset/--pieas-only", default=True, help="Also wipe OpenEduCat + sync store.")
def seed(students: int, faculty: int, courses: int, seed: int, full_reset: bool):
    """(Re)create the dummy PIEAS database with fake data."""
    conn = reset_pieas_db(PIEAS_DB_PATH)
    counts = seed_pieas(conn, students, faculty, courses, seed)
    conn.close()
    console.print(f"[green]Seeded PIEAS DB[/green]: {counts}")
    if full_reset:
        reset_oc_db(OPENEDUCAT_DB_PATH).close()
        sync_store.reset_database(SYNC_STORE_DB_PATH)
        console.print("[green]Reset OpenEduCat mock + orchestrator state store[/green]")


@cli.command()
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
def migrate(entity: str):
    """Run the one-time bulk migration cycle."""
    orchestrator, extractor, loader, validator = _build_agents()
    for et in _entity_list(entity):
        if not orchestrator.is_bulk_mode(et):
            console.print(f"[yellow]{et}: already migrated (sync_mapping is non-empty) -- skipping bulk, "
                          f"run 'sync' instead[/yellow]")
            continue
        report = run_cycle("bulk", et, orchestrator, extractor, loader, validator)
        _print_report("BULK MIGRATION", report)
    extractor.close(); loader.close(); orchestrator.close()


@cli.command()
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
@click.option("--loop/--once", default=False, help="Keep running on an interval instead of a single pass.")
@click.option("--interval", default=CHANGE_CYCLE_INTERVAL_SECONDS, show_default=True, help="Seconds between loop passes.")
def sync(entity: str, loop: bool, interval: int):
    """Run the change-detection cycle (timestamp watermark)."""
    orchestrator, extractor, loader, validator = _build_agents()
    try:
        while True:
            for et in _entity_list(entity):
                report = run_cycle("change", et, orchestrator, extractor, loader, validator)
                _print_report("CHANGE CYCLE", report)
            if not loop:
                break
            console.print(f"[dim]sleeping {interval}s...[/dim]")
            time.sleep(interval)
    finally:
        extractor.close(); loader.close(); orchestrator.close()


@cli.command(name="deletion-check")
@click.option("--entity", type=click.Choice(list(ENTITY_TYPES) + ["all"]), default="all")
def deletion_check(entity: str):
    """Run the deletion-detection cycle (full ID list vs. sync_mapping)."""
    orchestrator, extractor, loader, validator = _build_agents()
    for et in _entity_list(entity):
        report = run_cycle("deletion", et, orchestrator, extractor, loader, validator)
        _print_report("DELETION CYCLE", report)
    extractor.close(); loader.close(); orchestrator.close()


@cli.command()
@click.option("--entity", type=click.Choice(["student", "faculty", "course"]), default="student")
@click.option("--update", "n_update", default=3, show_default=True, help="Rows to edit.")
@click.option("--insert", "n_insert", default=2, show_default=True, help="New rows to add.")
@click.option("--delete", "n_delete", default=1, show_default=True, help="Rows to remove.")
@click.option("--seed", default=None, type=int, help="RNG seed for reproducible mutation.")
def mutate(entity: str, n_update: int, n_insert: int, n_delete: int, seed: int | None):
    """Simulate PIEAS 'still being used': edits, new admissions, and removals."""
    rng = random.Random(seed)
    conn = src.get_connection(PIEAS_DB_PATH)
    table = PIEAS_TABLE_FOR_ENTITY[entity]
    ids = src.fetch_ids(conn, table)
    if not ids:
        console.print("[red]No rows to mutate -- run 'seed' first.[/red]")
        return

    updated, deleted = [], []
    for pieas_id in rng.sample(ids, k=min(n_update, len(ids))):
        if entity == "student":
            src.update_fields(conn, table, pieas_id, {"department": rng.choice(
                ["Computer Science", "Electrical Engineering", "Physics"])})
        elif entity == "faculty":
            src.update_fields(conn, table, pieas_id, {"designation": rng.choice(
                ["Lecturer", "Assistant Professor", "Associate Professor", "Professor"])})
        else:
            src.update_fields(conn, table, pieas_id, {"credit_hours": rng.choice([2, 3, 4])})
        updated.append(pieas_id)

    remaining = [i for i in ids if i not in updated]
    for pieas_id in rng.sample(remaining, k=min(n_delete, len(remaining))):
        src.delete_row(conn, table, pieas_id)
        deleted.append(pieas_id)

    inserted = []
    if entity == "student":
        existing = src.count_rows(conn, "students") + 100000
        for i in range(n_insert):
            new_id = f"PIEAS-STU-{existing + i:06d}"
            src.insert_student(conn, PieasStudent(
                pieas_id=new_id, roll_number=f"2026-CS-{existing + i}",
                first_name="New", last_name=f"Admit{i}", email=f"new.admit{i}.{existing}@example.com",
                gender=rng.choice(["male", "female"]), date_of_birth="2005-01-01",
                department="Computer Science", batch_year=2026,
                last_updated=datetime.now(timezone.utc),
            ))
            inserted.append(new_id)

    console.print(f"[cyan]{entity}[/cyan]: updated={updated} deleted={deleted} inserted={inserted}")
    conn.close()


@cli.command()
def status():
    """Print current row counts and watermarks across all three databases."""
    orchestrator, extractor, loader, validator = _build_agents()
    pieas_conn = src.get_connection(PIEAS_DB_PATH)
    oc_client = loader._client

    table = Table(title="OpenEdu Orchestrator -- status")
    table.add_column("Entity")
    table.add_column("PIEAS rows", justify="right")
    table.add_column("OpenEduCat active", justify="right")
    table.add_column("OpenEduCat archived", justify="right")
    table.add_column("sync_mapping rows", justify="right")
    table.add_column("watermark")

    for et in ENTITY_TYPES:
        pieas_table = PIEAS_TABLE_FOR_ENTITY[et]
        model = OPENEDUCAT_MODEL_FOR_ENTITY[et]
        pieas_count = src.count_rows(pieas_conn, pieas_table)
        active_count = len(oc_client.search_read(model, [("active", "=", True)]))
        archived_count = len(oc_client.search_read(model, [("active", "=", False)]))
        mapping_count = orchestrator.mapping_count(et)
        watermark = orchestrator.get_watermark(et)
        table.add_row(
            et, str(pieas_count), str(active_count), str(archived_count),
            str(mapping_count), watermark.isoformat() if watermark else "-",
        )
    console.print(table)
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
