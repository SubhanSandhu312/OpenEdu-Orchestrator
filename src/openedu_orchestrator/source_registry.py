"""Registry of source systems this pipeline can port data from.

Generalizes what used to be hardcoded PIEAS-only globals
(PIEAS_MODEL_FOR_ENTITY, PIEAS_TABLE_FOR_ENTITY, PIEAS_DB_PATH) into a
lookup keyed by source-system name. Adding a real second university means
adding one entry here, an adapter module satisfying source_adapter.SourceAdapter,
and Pydantic model(s) describing its schema -- nothing else in the pipeline
(ExtractorAgent, mapping_authoring.py) needs to change, since both already
take their source module/schema as parameters rather than importing
pieas_source directly.

"pieas" remains the default everywhere for backward compatibility -- every
existing call site that doesn't pass source_system explicitly keeps
behaving exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from openedu_orchestrator import example_univ_source, pieas_source
from openedu_orchestrator.config import PIEAS_DB_PATH, PIEAS_TABLE_FOR_ENTITY
from openedu_orchestrator.models import PIEAS_MODEL_FOR_ENTITY, ExampleUnivStudent
from openedu_orchestrator.source_adapter import check_adapter


@dataclass(frozen=True)
class SourceSystemSpec:
    name: str
    adapter: Any  # a module satisfying source_adapter.SourceAdapter
    model_for_entity: dict[str, type]
    table_for_entity: dict[str, str]
    default_conn_info: Optional[Any] = None


SOURCE_SYSTEMS: dict[str, SourceSystemSpec] = {
    "pieas": SourceSystemSpec(
        name="pieas",
        adapter=pieas_source,
        model_for_entity=PIEAS_MODEL_FOR_ENTITY,
        table_for_entity=PIEAS_TABLE_FOR_ENTITY,
        default_conn_info=PIEAS_DB_PATH,
    ),
    "example_univ": SourceSystemSpec(
        name="example_univ",
        adapter=example_univ_source,
        model_for_entity={"student": ExampleUnivStudent},
        table_for_entity={"student": "students"},
        default_conn_info=None,  # caller supplies a path explicitly; no fixed default file
    ),
}

for _spec in SOURCE_SYSTEMS.values():
    check_adapter(_spec.adapter)


def get_source_system(name: str) -> SourceSystemSpec:
    if name not in SOURCE_SYSTEMS:
        raise ValueError(f"Unknown source system {name!r}. Registered: {sorted(SOURCE_SYSTEMS)}")
    return SOURCE_SYSTEMS[name]
