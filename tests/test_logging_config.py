from __future__ import annotations

import json
import logging

from openedu_orchestrator.logging_config import JsonFormatter


def _make_record(msg="cycle_completed", extra=None, level=logging.INFO):
    record = logging.LogRecord(
        name="openedu_orchestrator.graph", level=level, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def test_formats_as_valid_json_with_core_fields():
    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "cycle_completed"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "openedu_orchestrator.graph"
    assert "timestamp" in payload


def test_extra_fields_are_folded_into_the_json_payload():
    record = _make_record(extra={"mode": "change", "entity_type": "student", "fetched": 3})
    payload = json.loads(JsonFormatter().format(record))
    assert payload["mode"] == "change"
    assert payload["entity_type"] == "student"
    assert payload["fetched"] == 3


def test_exception_info_is_included_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="x", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="load_write_failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exception"]
    assert "boom" in payload["exception"]
