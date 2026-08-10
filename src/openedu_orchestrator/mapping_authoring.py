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

Model provider: Google Gemini (free tier via https://aistudio.google.com/apikey
-- no paid API key required, since this runs once per source system rather
than per record). Requires GEMINI_API_KEY in the environment.
"""

from __future__ import annotations

import json
import os
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


_SYSTEM_INSTRUCTION = (
    "You are assisting a data-migration engineer in mapping fields from a "
    "source university system's schema onto a real OpenEduCat/Odoo target "
    "schema. Be conservative and honest about uncertainty:\n"
    "- Only propose 'direct' or 'value_map' when you are genuinely "
    "confident the mapping is correct.\n"
    "- If a source field has no clear target equivalent, or the target "
    "field is relational (a many2one/one2many needing linked records "
    "rather than a plain value -- e.g. a department or batch reached "
    "through an enrollment record, not a direct field), mark it "
    "'unmapped' and explain why in `note`. Do not guess a mapping just to "
    "avoid leaving a field unmapped.\n"
    "- If a target field is required (per its schema's `required` flag) "
    "but no source field is a good match, list it under "
    "`unmapped_required_target_fields`. NEVER invent a plausible-sounding "
    "default value to fill a gap -- that would silently fabricate data in "
    "a real system of record.\n"
    "- If a target field's `selection` list differs from the source's "
    "raw values (e.g. 'male'/'female' vs 'm'/'f'), use 'value_map' and "
    "give the exact translation, not just a `direct` passthrough."
)


def _call_llm(prompt: str) -> str:
    """Calls Google Gemini (free tier -- no paid key needed, since this
    runs once per source system, not per record) with structured output
    against the MappingProposal schema. Requires GEMINI_API_KEY in the
    environment; get one free (no credit card) at
    https://aistudio.google.com/apikey.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key (no credit card) at "
            "https://aistudio.google.com/apikey, then set it as an "
            "environment variable and retry. See "
            "docs/mapping_authoring_tool.md."
        )
    from google import genai

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config={
            "system_instruction": _SYSTEM_INSTRUCTION,
            "response_mime_type": "application/json",
            "response_schema": MappingProposal,
        },
    )
    return response.text


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
