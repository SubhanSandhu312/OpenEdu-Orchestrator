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
