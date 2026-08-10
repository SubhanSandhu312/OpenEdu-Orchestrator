"""Typed data shapes shared across agents.

Keeping these as pydantic models (rather than passing raw dicts everywhere) means
a malformed record from any layer fails loudly at a model boundary instead of
silently corrupting a downstream write -- the same failure mode the report
flags as the risk of bypassing an ORM.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

EntityType = Literal["student", "faculty", "course"]
SyncAction = Literal["create", "update", "unchanged", "archive"]
SyncMode = Literal["bulk", "change", "deletion"]


class PieasStudent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pieas_id: str
    roll_number: str
    first_name: str
    last_name: str
    email: str
    gender: str
    date_of_birth: date
    department: str
    batch_year: int
    last_updated: datetime


class PieasFaculty(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pieas_id: str
    employee_code: str
    first_name: str
    last_name: str
    email: str
    department: str
    designation: str
    last_updated: datetime


class PieasCourse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pieas_id: str
    code: str
    name: str
    department: str
    credit_hours: int
    semester: str
    last_updated: datetime


PIEAS_MODEL_FOR_ENTITY: dict[str, type[BaseModel]] = {
    "student": PieasStudent,
    "faculty": PieasFaculty,
    "course": PieasCourse,
}


class SyncMappingRow(BaseModel):
    id: Optional[int] = None
    pieas_id: str
    openeducat_id: int
    entity_type: EntityType
    content_hash: str
    last_synced_at: datetime


class ClassifiedRecord(BaseModel):
    """One PIEAS row plus the Orchestrator's decision about what to do with it."""

    entity_type: EntityType
    pieas_id: str
    action: SyncAction
    source_record: dict
    openeducat_id: Optional[int] = None
    content_hash: Optional[str] = None


class LoadResult(BaseModel):
    entity_type: EntityType
    pieas_id: str
    action: SyncAction
    openeducat_id: int
    ok: bool
    error: Optional[str] = None


FieldHandling = Literal["direct", "value_map", "external_id", "unmapped"]


class FieldMappingEntry(BaseModel):
    """One source field's disposition when mapping into a real target schema.

    `direct`: passthrough or rename (source_field -> target_field, same value).
    `value_map`: enum/selection translation (e.g. PIEAS "male" -> Odoo "m").
    `external_id`: routed to the client's ir.model.data handling, same as
    pieas_id today -- not written as a plain field at all.
    `unmapped`: flagged, not guessed -- either no target equivalent exists,
    or the target field is relational (needs FK/linking logic, not a value
    mapping). `note` should say which.
    """

    source_field: str
    target_field: Optional[str] = None
    handling: FieldHandling = "direct"
    value_map: Optional[dict[str, str]] = None
    note: Optional[str] = None


class UnmappedRequiredTargetField(BaseModel):
    """A target field with required=True (per fields_get) that has no
    candidate source field at all -- a data-availability gap, not something
    a mapping can fix. Surfacing this explicitly is how the faculty-gender
    gap (PIEAS has no gender field; op.faculty requires one) would have
    been caught by inspection instead of by a failed live write.
    """

    target_field: str
    target_label: str
    note: str


class MappingProposal(BaseModel):
    """Draft output of propose_mapping(); always requires human review
    before compile_mapping() turns it into an executable transform_fn.
    """

    entity_type: EntityType
    target_model: str
    field_mappings: list[FieldMappingEntry]
    unmapped_required_target_fields: list[UnmappedRequiredTargetField] = []


class RunReport(BaseModel):
    mode: SyncMode
    entity_type: EntityType
    started_at: datetime
    finished_at: Optional[datetime] = None
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    archived: int = 0
    errors: list[str] = []
    validation_issues: list[str] = []
    watermark_before: Optional[datetime] = None
    watermark_after: Optional[datetime] = None
    pages: int = 0
