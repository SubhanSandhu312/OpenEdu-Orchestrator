"""Design-time mapping-authoring tool -- NOT part of the live sync pipeline.

Scope and rationale: docs/mapping_authoring_tool.md. Short version: real
testing against a live OpenEduCat instance showed every field mismatch we
hit (roll_number -> gr_no, inconsistent gender encodings, missing faculty
gender data, relational department/batch fields) was discoverable via
schema introspection (fields_get) plus a static lookup -- not something
needing live natural-language judgment per record. So the LLM's role here
is authoring a draft mapping once per new source system, for a human to
review and approve; the approved, static config is what actually runs
(compile_mapping), with zero LLM involvement at sync time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from openedu_orchestrator.models import (
    PIEAS_MODEL_FOR_ENTITY,
    FieldMappingEntry,
    MappingProposal,
    UnmappedRequiredTargetField,
)


def extract_source_schema(entity_type: str) -> dict[str, dict[str, Any]]:
    """Field name -> {type, required} from the Pydantic model already
    defined for this entity type in models.py. Every source system this
    project supports defines one of these (PIEAS_MODEL_FOR_ENTITY today;
    a future adapter for another university would add its own) -- no new
    source-schema infrastructure is needed to support this tool.
    """
    if entity_type not in PIEAS_MODEL_FOR_ENTITY:
        raise ValueError(f"No source model registered for entity_type={entity_type!r}")
    model = PIEAS_MODEL_FOR_ENTITY[entity_type]
    return {
        name: {"type": str(field.annotation), "required": field.is_required()}
        for name, field in model.model_fields.items()
    }


def extract_target_schema(client: Any, target_model: str) -> dict[str, dict[str, Any]]:
    """Field name -> {label, type, required, selection} from the real
    target model, via the same fields_get call used to hand-diagnose
    today's real mismatches. `client` needs only an `_execute(model,
    method, args, kwargs)` method -- OdooXmlRpcClient already has one.
    """
    fields = client._execute(
        target_model,
        "fields_get",
        [],
        {"attributes": ["string", "type", "required", "selection"]},
    )
    return {
        name: {
            "label": info.get("string", ""),
            "type": info.get("type", ""),
            "required": bool(info.get("required")),
            "selection": info.get("selection"),
        }
        for name, info in fields.items()
    }


def build_mapping_prompt(
    entity_type: str,
    source_schema: dict[str, dict[str, Any]],
    target_schema: dict[str, dict[str, Any]],
    sample_records: Optional[list[dict]] = None,
) -> str:
    """Assemble the prompt context propose_mapping() would send. Kept as a
    separate, inspectable function so the exact context an eventual model
    call sees can be reviewed/tested without making a live call.
    """
    lines = [
        f"Entity type: {entity_type}",
        "",
        "Source schema (from the source system's own model):",
        json.dumps(source_schema, indent=2),
        "",
        "Target schema (from the real OpenEduCat/Odoo model's fields_get):",
        json.dumps(target_schema, indent=2),
    ]
    if sample_records:
        lines += ["", "Sample source records:", json.dumps(sample_records, indent=2, default=str)]
    lines += [
        "",
        "For each source field, decide: direct mapping/rename, value_map "
        "(if the target uses a different value domain -- check the target "
        "field's `selection` list), external_id (if it's the source's own "
        "primary key/foreign-system identifier), or unmapped (if no target "
        "field corresponds, or the target field is relational and needs "
        "linking logic rather than a value mapping -- say which in `note`). "
        "Separately, list every target field with required=true that has "
        "no reasonable source field at all, as unmapped_required_target_fields "
        "-- do not invent a default value for these.",
    ]
    return "\n".join(lines)


def _call_llm(prompt: str) -> str:
    """The one seam this module leaves unwired. No ANTHROPIC_API_KEY is
    available in this environment to verify a live call against, so this
    raises rather than shipping unverified prompt-engineering as if it
    were tested. To wire it up: call Claude with structured output
    (tool-use / response schema) against the MappingProposal pydantic
    schema, using build_mapping_prompt()'s output as context. See
    docs/mapping_authoring_tool.md for the full design.
    """
    raise NotImplementedError(
        "_call_llm is not wired to a live model yet -- no ANTHROPIC_API_KEY "
        "was available to verify a real call against. See "
        "docs/mapping_authoring_tool.md for what's needed to wire it up."
    )


def propose_mapping(
    entity_type: str,
    client: Any,
    target_model: str,
    sample_records: Optional[list[dict]] = None,
) -> MappingProposal:
    """Draft a mapping proposal for a human to review. Never called from
    the live sync pipeline -- run once per new source system or target
    schema change.
    """
    source_schema = extract_source_schema(entity_type)
    target_schema = extract_target_schema(client, target_model)
    prompt = build_mapping_prompt(entity_type, source_schema, target_schema, sample_records)
    raw = _call_llm(prompt)
    return MappingProposal.model_validate_json(raw)


def compile_mapping(config: dict) -> Callable[[str, dict], dict]:
    """Turn an approved mapping config (a human-reviewed MappingProposal,
    as a plain dict -- e.g. loaded via load_mapping()) into a deterministic
    transform_fn compatible with graph.py's transform_fn injection point.
    No LLM involvement at all -- this only ever reads an already-approved
    static config.
    """
    field_mappings = config["field_mappings"]

    def _transform(entity_type: str, record: dict) -> dict:
        out: dict[str, Any] = {}
        for fm in field_mappings:
            handling = fm.get("handling", "direct")
            if handling == "unmapped":
                continue
            value = record.get(fm["source_field"])
            if handling == "value_map":
                value_map = fm.get("value_map") or {}
                value = value_map.get(value, value)
            if handling == "external_id":
                # Matches OdooXmlRpcClient.create()'s special handling: it
                # pops "pieas_id" out of the values dict itself and
                # registers it via ir.model.data rather than writing it as
                # a plain field.
                out["pieas_id"] = value
                continue
            target_field = fm.get("target_field") or fm["source_field"]
            out[target_field] = value
        return out

    return _transform


def save_mapping(proposal: MappingProposal, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.model_dump(), indent=2))


def load_mapping(path: Path) -> dict:
    return json.loads(path.read_text())
