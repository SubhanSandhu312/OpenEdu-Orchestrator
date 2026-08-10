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

EntityType = Literal["student", "faculty", "course", "mark"]
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
    gender: str
    date_of_birth: date
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


class PieasMark(BaseModel):
    """An exam/quiz mark -- the report's own worked example of an ongoing
    change ("a quiz mark is updated", Section 2).

    Unlike the other three, this is a *relational* record: it is meaningless
    without the student and the subject it points at, both of which must
    already exist on the target before it can be written. That dependency is
    why ENTITY_TYPES is an ordered tuple with "mark" last, and why the
    reference resolver raises a clear error rather than writing a dangling
    foreign key (see real_target_reference_data._exam_attendee_references).
    """

    model_config = ConfigDict(from_attributes=True)

    pieas_id: str
    student_pieas_id: str
    course_pieas_id: str
    exam_name: str
    marks_obtained: int
    total_marks: int
    last_updated: datetime


PIEAS_MODEL_FOR_ENTITY: dict[str, type[BaseModel]] = {
    "student": PieasStudent,
    "faculty": PieasFaculty,
    "course": PieasCourse,
    "mark": PieasMark,
}


class ExampleUnivStudent(BaseModel):
    """A second, genuinely different source schema -- proves the adapter
    pattern generalizes rather than just being renamed PIEAS. Different
    field names (student_ref/given_name/sex/dob/major/intake_year, not
    roll_number/first_name/gender/date_of_birth/department/batch_year)
    AND a different value convention (sex: 'M'/'F' single letters, not
    gender: 'male'/'female' full words) -- deliberately, so the
    mapping-authoring tool has to do real translation work, not just
    match already-identical PIEAS-shaped names.

    "Example University" is a deliberately generic placeholder, not a
    stand-in for any specific real institution.
    """

    model_config = ConfigDict(from_attributes=True)

    student_ref: str
    given_name: str
    family_name: str
    contact_email: str
    sex: str
    dob: date
    major: str
    intake_year: int
    updated_at: datetime


class SyncMappingRow(BaseModel):
    id: Optional[int] = None
    source_system: str
    source_id: str
    openeducat_id: int
    entity_type: EntityType
    content_hash: str
    last_synced_at: datetime


class ClassifiedRecord(BaseModel):
    """One source row plus the Orchestrator's decision about what to do with it."""

    entity_type: EntityType
    source_id: str
    action: SyncAction
    source_record: dict
    openeducat_id: Optional[int] = None
    content_hash: Optional[str] = None


class LoadResult(BaseModel):
    entity_type: EntityType
    source_id: str
    action: SyncAction
    openeducat_id: int
    ok: bool
    error: Optional[str] = None


FieldHandling = Literal["direct", "value_map", "external_id", "reference", "unmapped"]


class ValueMapEntry(BaseModel):
    source_value: str
    target_value: str


class FieldMappingEntry(BaseModel):
    """One source field's disposition when mapping into a real target schema.

    `direct`: passthrough or rename (source_field -> target_field, same value).
    `value_map`: enum/selection translation (e.g. PIEAS "male" -> Odoo "m").
    `external_id`: routed to the client's ir.model.data handling, same as
    pieas_id today -- not written as a plain field at all.
    `reference`: names a *shared* target record (a department, a subject, an
    exam) that many source rows point at, rather than a value belonging to
    this row. Passed through to the client as a sentinel and resolved
    get-or-create style against the target -- see
    real_target_reference_data.py. Use this where `unmapped` would otherwise
    be used with a "relational, needs FK logic" note.
    `unmapped`: flagged, not guessed -- no target equivalent exists at all.
    `note` should say why.

    `value_map` is a list of explicit (source_value, target_value) pairs,
    not a plain dict -- Gemini's free Developer API structured-output mode
    rejects JSON schemas using `additionalProperties` (the schema a plain
    `dict[str, str]` field generates), found by an actual live API call,
    not anticipated in advance.
    """

    source_field: str
    target_field: Optional[str] = None
    handling: FieldHandling = "direct"
    value_map: Optional[list[ValueMapEntry]] = None
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
