from __future__ import annotations

import pytest

from openedu_orchestrator.agents.extractor import ExtractorAgent


@pytest.fixture
def extractor(dbs):
    ex = ExtractorAgent(dbs.pieas_path)
    yield ex
    ex.close()


def test_fetch_changed_instruction(extractor):
    result = extractor.fetch({"op": "fetch_changed", "entity_type": "student", "watermark": None})
    assert len(result["records"]) == 12
    assert "records" in result and "ids" not in result


def test_fetch_page_instruction(extractor):
    result = extractor.fetch({"op": "fetch_page", "entity_type": "student", "limit": 5, "offset": 0})
    assert len(result["records"]) == 5
    assert result["returned"] == 5


def test_fetch_ids_instruction(extractor):
    result = extractor.fetch({"op": "fetch_ids", "entity_type": "faculty"})
    assert len(result["ids"]) == 5
    assert "records" not in result


def test_unknown_instruction_raises(extractor):
    with pytest.raises(ValueError):
        extractor.fetch({"op": "bogus", "entity_type": "student"})


def test_extractor_holds_no_state_between_calls(extractor):
    """Two identical fetch_changed(watermark=None) calls must return the same
    thing -- the Extractor must not remember or filter based on prior calls.
    """
    first = extractor.fetch({"op": "fetch_changed", "entity_type": "student", "watermark": None})
    second = extractor.fetch({"op": "fetch_changed", "entity_type": "student", "watermark": None})
    assert first == second
