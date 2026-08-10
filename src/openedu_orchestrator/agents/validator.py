"""Validation Agent (optional extension, Section 3.4).

"After a Loader write, it re-reads the affected record back from OpenEduCat
and confirms it matches what was sent... This is a quality-assurance
addition, not part of the core decision loop, and does not change any of the
responsibilities above -- it observes after the fact rather than
participating in the extract/decide/write flow."

It therefore only ever reads (via the Loader's read-back helper) and never
writes, and its findings are advisory: they are appended to the run report,
they never block or retry a write themselves.
"""

from __future__ import annotations

from openedu_orchestrator.agents.loader import LoaderAgent


class ValidationAgent:
    def __init__(self, loader: LoaderAgent):
        self._loader = loader

    @staticmethod
    def _matches(expected, actual) -> bool:
        """A cleared field is written as None but reads back as False.

        OdooXmlRpcClient._marshal_values converts None -> False on the way
        out, because XML-RPC cannot serialise None and False is Odoo's own
        canonical empty value. So clearing a field and then reading it back
        legitimately returns False where None was sent. Those are the same
        value expressed on two sides of a boundary, not a mismatch --
        without this, every field-clearing write would report a spurious
        validation issue.
        """
        if expected is None and actual is False:
            return True
        return actual == expected

    def validate_write(self, entity_type: str, openeducat_id: int, expected_fields: dict) -> str | None:
        actual = self._loader.read_back(entity_type, openeducat_id)
        if actual is None:
            return f"{entity_type}/{openeducat_id}: record missing after write"
        # "source_id" is a client-internal sentinel (OdooXmlRpcClient pops
        # it before writing and registers it via ir.model.data instead --
        # see mapping_authoring.py's compile_mapping); it is never actually
        # persisted as a real field on any target, mock or real, so it can
        # never match on read-back. Found as a real false-positive
        # validation failure the first time this ran through the real
        # target's compiled mapping with a validator attached.
        mismatches = [
            f"{field}=expected {value!r}, got {actual.get(field)!r}"
            for field, value in expected_fields.items()
            if field != "source_id" and not self._matches(value, actual.get(field))
        ]
        if mismatches:
            return f"{entity_type}/{openeducat_id}: " + "; ".join(mismatches)
        return None

    def validate_archive(self, entity_type: str, openeducat_id: int) -> str | None:
        actual = self._loader.read_back(entity_type, openeducat_id)
        if actual is None:
            return f"{entity_type}/{openeducat_id}: record missing after archive"
        if actual.get("active") not in (0, False):
            return f"{entity_type}/{openeducat_id}: expected active=False after archive, got {actual.get('active')!r}"
        return None

    def validate_batch(self, entity_type: str, writes: list[dict]) -> list[str]:
        """writes: [{openeducat_id, fields, action}] for successfully-loaded create/update items."""
        issues = []
        for item in writes:
            issue = self.validate_write(entity_type, item["openeducat_id"], item["fields"])
            if issue:
                issues.append(issue)
        return issues

    def validate_archives(self, entity_type: str, archived_ids: list[int]) -> list[str]:
        issues = []
        for openeducat_id in archived_ids:
            issue = self.validate_archive(entity_type, openeducat_id)
            if issue:
                issues.append(issue)
        return issues
