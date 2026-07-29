from __future__ import annotations

from openedu_orchestrator.graph import run_cycle


def test_bulk_migration_paginates_until_exhausted(dbs, agents):
    report = run_cycle(
        "bulk", "student", agents.orchestrator, agents.extractor, agents.loader, agents.validator,
        page_size=5,
    )
    assert report.fetched == 12
    assert report.created == 12
    assert report.updated == 0
    assert report.pages == 3  # 5 + 5 + 2
    assert report.errors == []
    assert report.validation_issues == []
    assert agents.orchestrator.mapping_count("student") == 12
    assert report.watermark_after is not None


def test_bulk_migration_creates_active_records_in_openeducat(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader, page_size=5)
    active = dbs.oc_client.search_read("op.student", [("active", "=", True)])
    assert len(active) == 12


def test_bulk_migration_scoped_per_entity_type(dbs, agents):
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader)
    assert agents.orchestrator.is_bulk_mode("student") is False
    assert agents.orchestrator.is_bulk_mode("faculty") is True  # untouched

    run_cycle("bulk", "faculty", agents.orchestrator, agents.extractor, agents.loader)
    assert agents.orchestrator.mapping_count("faculty") == 5
    assert agents.orchestrator.mapping_count("student") == 12  # unaffected by faculty run


def test_rerunning_bulk_migration_is_idempotent(dbs, agents):
    """Re-running the bulk pipeline over the same unchanged source data must
    not create duplicate OpenEduCat records or duplicate mapping rows --
    every record's content hash still matches, so everything classifies as
    'unchanged' the second time.
    """
    run_cycle("bulk", "student", agents.orchestrator, agents.extractor, agents.loader, page_size=5)
    first_count = len(dbs.oc_client.search_read("op.student"))

    second_report = run_cycle(
        "bulk", "student", agents.orchestrator, agents.extractor, agents.loader, page_size=5
    )
    second_count = len(dbs.oc_client.search_read("op.student"))

    assert second_report.created == 0
    assert second_report.unchanged == 12
    assert second_count == first_count == 12
    assert agents.orchestrator.mapping_count("student") == 12
