from __future__ import annotations

from types import SimpleNamespace

import pytest

from openedu_orchestrator import sync_store
from openedu_orchestrator.agents.extractor import ExtractorAgent
from openedu_orchestrator.agents.loader import LoaderAgent
from openedu_orchestrator.agents.orchestrator import OrchestratorAgent
from openedu_orchestrator.agents.validator import ValidationAgent
from openedu_orchestrator.openeducat_client import reset_database as reset_oc_db
from openedu_orchestrator.pieas_source import reset_database as reset_pieas_db
from openedu_orchestrator.seed import seed_pieas


@pytest.fixture
def dbs(tmp_path):
    """Three isolated, freshly-created SQLite files per test -- no shared
    state with the demo data/ directory or with other tests.
    """
    pieas_path = tmp_path / "pieas.db"
    oc_path = tmp_path / "openeducat_mock.db"
    sync_path = tmp_path / "orchestrator_state.db"

    pieas_conn = reset_pieas_db(pieas_path)
    seed_pieas(pieas_conn, num_students=12, num_faculty=5, num_courses=4, seed=1)
    oc_client = reset_oc_db(oc_path)
    sync_store.reset_database(sync_path)

    ns = SimpleNamespace(
        pieas_path=pieas_path, oc_path=oc_path, sync_path=sync_path,
        pieas_conn=pieas_conn, oc_client=oc_client,
    )
    yield ns
    pieas_conn.close()
    oc_client.close()


@pytest.fixture
def agents(dbs):
    orchestrator = OrchestratorAgent(dbs.sync_path)
    extractor = ExtractorAgent(dbs.pieas_path)
    loader = LoaderAgent(dbs.oc_client)
    validator = ValidationAgent(loader)
    ns = SimpleNamespace(orchestrator=orchestrator, extractor=extractor, loader=loader, validator=validator)
    yield ns
    orchestrator.close()
    extractor.close()
