"""Structured logging for the pipeline.

The CLI's Rich console output (see cli.py's _print_report) is for a human
watching a terminal interactively. This is separate and complementary: one
JSON object per log line, meant to be shipped to a log aggregator (e.g.
CloudWatch, Datadog, an ELK stack) in a production deployment, where the
operator is *not* watching a terminal. Stdlib `logging` only, so this adds
no new dependency.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Reserved LogRecord attributes -- anything else set via `extra={...}` is
# pipeline-specific structured data and gets folded into the JSON output.
_STANDARD_LOG_RECORD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(level: str | None = None) -> None:
    """Idempotent: safe to call from every CLI command's entry point without
    ending up with duplicate handlers (each `cli()` invocation is a fresh
    process, but tests that import the CLI module repeatedly are not).
    """
    global _configured
    if _configured:
        return
    resolved_level = (level or os.environ.get("OPENEDU_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger("openedu_orchestrator")
    root.setLevel(resolved_level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
