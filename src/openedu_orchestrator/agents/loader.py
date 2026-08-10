"""Loader Agent -- writes into OpenEduCat, and only via the ORM-shaped client.

Per the report (Section 3.3 / Section 7): writes go through OpenEduCat's ORM
(XML-RPC in production; here, the equivalently-shaped OpenEduCatClient) so
computed fields and validation behave correctly. The Loader executes exactly
the action it is told -- create, write (update), or write with active=False
(archive) -- and makes no decisions of its own about which action applies.
"""

from __future__ import annotations

from pathlib import Path

from openedu_orchestrator.config import OPENEDUCAT_DB_PATH, OPENEDUCAT_MODEL_FOR_ENTITY
from openedu_orchestrator.logging_config import get_logger
from openedu_orchestrator.models import LoadResult
from openedu_orchestrator.openeducat_client import OpenEduCatClient

logger = get_logger(__name__)


class LoaderAgent:
    def __init__(
        self,
        client: OpenEduCatClient | None = None,
        db_path: Path = OPENEDUCAT_DB_PATH,
        model_for_entity: dict | None = None,
    ):
        self._client = client or OpenEduCatClient(db_path)
        self._owns_client = client is None
        # Defaults to the shared entity->model map (what the mock's
        # SQLite tables are keyed by, and what the tests assume) so
        # nothing changes unless explicitly overridden -- needed because
        # PIEAS's "courses" turned out to map onto real op.subject, not
        # op.course, but the mock target still uses op.course and must
        # stay untouched.
        self._model_for_entity = model_for_entity or OPENEDUCAT_MODEL_FOR_ENTITY

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def apply(
        self,
        entity_type: str,
        action: str,
        fields: dict,
        source_id: str,
        openeducat_id: int | None = None,
    ) -> LoadResult:
        model = self._model_for_entity[entity_type]
        try:
            if action == "create":
                new_id = self._client.create(model, fields)
                return LoadResult(
                    entity_type=entity_type, source_id=source_id, action=action,
                    openeducat_id=new_id, ok=True,
                )
            if action == "update":
                if openeducat_id is None:
                    raise ValueError("update requires an existing openeducat_id")
                self._client.write(model, openeducat_id, fields)
                return LoadResult(
                    entity_type=entity_type, source_id=source_id, action=action,
                    openeducat_id=openeducat_id, ok=True,
                )
            if action == "archive":
                if openeducat_id is None:
                    raise ValueError("archive requires an existing openeducat_id")
                self._client.archive(model, openeducat_id)
                return LoadResult(
                    entity_type=entity_type, source_id=source_id, action=action,
                    openeducat_id=openeducat_id, ok=True,
                )
            raise ValueError(f"Unknown load action: {action!r}")
        except Exception as exc:  # noqa: BLE001 -- surfaced in RunReport.errors, not swallowed
            logger.error(
                "load_write_failed",
                extra={"entity_type": entity_type, "action": action, "source_id": source_id,
                       "openeducat_id": openeducat_id, "exception": str(exc)},
            )
            return LoadResult(
                entity_type=entity_type, source_id=source_id, action=action,
                openeducat_id=openeducat_id or -1, ok=False, error=str(exc),
            )

    def apply_batch(self, entity_type: str, classified_and_transformed: list[dict]) -> list[LoadResult]:
        """Each item: {action, source_id, openeducat_id, fields}."""
        results = []
        for item in classified_and_transformed:
            results.append(
                self.apply(
                    entity_type=entity_type,
                    action=item["action"],
                    fields=item.get("fields", {}),
                    source_id=item["source_id"],
                    openeducat_id=item.get("openeducat_id"),
                )
            )
        return results

    def read_back(self, entity_type: str, openeducat_id: int) -> dict | None:
        """Used by the optional Validation Agent -- re-reads a record after a write."""
        model = self._model_for_entity[entity_type]
        rows = self._client.read(model, [openeducat_id])
        return rows[0] if rows else None
