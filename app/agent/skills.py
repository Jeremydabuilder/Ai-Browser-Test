"""Skills: a reusable instruction + tool-scope configuration, not a new way
to run the agent.

A Skill is data, not code: name/description/instructions, an optional tool
allowlist, an optional output schema, an optional preferred provider/model,
and optional default context. Running one still goes through the exact same
AgentSession/ToolRegistry/Missions/MCP/approval machinery every other task
in this app already uses - see AgentSession.set_tool_allowlist() and
MainWindow._run_skill(). Nothing here talks to a transport, a browser, or a
tool directly.

Built-in Skills are plain Python constants, never stored in the database and
never editable - "immutable except duplicate-as-custom" is true simply
because they are not rows in app.storage.skills.SkillStore at all. Editing
one always means Duplicate first, which copies its fields into a real,
mutable custom Skill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.automation.model import RecordedWorkflow

#: Bumped only if a stored custom Skill's shape changes in a way that needs
#: migrating old rows - see app/storage/database.py's schema migrations for
#: the same pattern applied to the database as a whole.
SKILL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    instructions: str
    #: None = every tool this window would normally offer (unrestricted,
    #: today's default behaviour for ordinary conversation). A tuple - even
    #: an empty one - is an explicit allowlist: entries are exactly the
    #: names ToolRegistry.schemas()/run() already use, so scoping to one
    #: MCP tool on one server needs no separate naming scheme - see
    #: ToolRegistry's allowed_tools parameter.
    allowed_tools: tuple[str, ...] | None = None
    #: A JSON Schema (dict) the model's final answer should match, or None
    #: for ordinary free-text output. Enforced by asking for it in the
    #: prompt and validating the answer afterwards (see validate_output) -
    #: none of this app's providers support provider-side constrained
    #: decoding today, and claiming otherwise would be exactly the "fake
    #: structured output" this phase's brief warns against.
    output_schema: dict | None = None
    #: A preference, never a hidden switch - see MainWindow._run_skill,
    #: which asks before running anywhere but the currently configured
    #: provider/model.
    preferred_provider: str = ""
    preferred_model: str = ""
    #: Hints for what @context should be pre-selected when this Skill is
    #: run - e.g. ("tab",) for "the current tab, if nothing else was
    #: explicitly selected". Consumed by MainWindow._run_skill via the same
    #: ContextComposer every other @context flow already uses; this is not
    #: a second context system, just a default for the first one.
    default_context_kinds: tuple[str, ...] = ()
    builtin: bool = False
    version: int = SKILL_SCHEMA_VERSION
    #: Phase 16: optional recorded-automation data. None for an ordinary
    #: prompt-only Skill (still the overwhelming majority). See
    #: app/automation/model.py's RecordedWorkflow - deliberately a field on
    #: Skill rather than a second, parallel "Automation" entity, so a
    #: recorded workflow is a Skill everywhere a Skill already works
    #: (Skills Library, Mission steps, Scheduled Tasks) with no duplicated
    #: plumbing.
    workflow: "RecordedWorkflow | None" = None

    @property
    def is_recorded_workflow(self) -> bool:
        return self.workflow is not None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "instructions": self.instructions,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "output_schema": self.output_schema,
            "preferred_provider": self.preferred_provider,
            "preferred_model": self.preferred_model,
            "default_context_kinds": list(self.default_context_kinds),
            "version": self.version,
            "workflow": self.workflow.as_dict() if self.workflow is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Skill":
        from app.automation.model import RecordedWorkflow

        allowed = data.get("allowed_tools")
        return cls(
            id=data["id"], name=data.get("name", ""), description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            allowed_tools=tuple(allowed) if allowed is not None else None,
            output_schema=data.get("output_schema"),
            preferred_provider=data.get("preferred_provider", ""),
            preferred_model=data.get("preferred_model", ""),
            default_context_kinds=tuple(data.get("default_context_kinds") or ()),
            builtin=False,
            version=int(data.get("version", SKILL_SCHEMA_VERSION)),
            workflow=RecordedWorkflow.from_dict(data.get("workflow")),
        )

    def duplicated(self, *, new_id: str, name: str | None = None) -> "Skill":
        """A mutable custom copy - the only way an immutable built-in ever
        becomes editable."""
        return replace(self, id=new_id, name=name or f"{self.name} (Copy)", builtin=False)


# ---------------------------------------------------------------------------
# Tool-allowlist building blocks, named for what they let a Skill do rather
# than repeating the same tuple of strings five times below.
# ---------------------------------------------------------------------------

READ_TOOLS = (
    "browser_get_page", "browser_get_page_text", "browser_get_pdf_text",
    "browser_find_elements", "browser_list_tabs", "browser_wait_for_element",
)
NAVIGATE_TOOLS = (
    "browser_navigate", "browser_open_tab", "browser_select_tab",
    "browser_back", "browser_forward", "browser_reload",
)
MISSION_TOOLS = (
    "mission_save_finding", "mission_note_source", "mission_save_question",
    "mission_resolve_question", "mission_save_decision", "mission_save_result",
    "mission_set_progress", "mission_save_constraints",
)

_COMPARE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "similarities": {"type": "array", "items": {"type": "string"}},
        "differences": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
}

_SUMMARIZE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points"],
}

BUILTIN_SKILLS: tuple[Skill, ...] = (
    Skill(
        id="deep_research",
        name="Deep Research",
        description="Follow links, read multiple sources, and record findings "
                    "against the active Mission.",
        instructions=(
            "Do deep research on the topic in my message and any context I've "
            "selected. Read multiple sources, follow relevant links, and use "
            "mission_save_finding to record each real discovery with its "
            "source. Use mission_note_source for pages you checked but that "
            "were not useful. When you are done, summarize what you found."),
        # Read + navigate + record findings - no form submission or page
        # mutation, since research is about reading, not acting on pages.
        allowed_tools=READ_TOOLS + NAVIGATE_TOOLS + ("browser_type",) + MISSION_TOOLS,
        default_context_kinds=("tab", "mission"),
        builtin=True,
    ),
    Skill(
        id="compare",
        name="Compare",
        description="Compare two or more selected tabs, PDFs, or files.",
        instructions=(
            "Compare the sources in my selected context (or my open tabs, if "
            "nothing is selected). Read each one, then report what they have "
            "in common and how they differ."),
        allowed_tools=READ_TOOLS,
        output_schema=_COMPARE_OUTPUT_SCHEMA,
        default_context_kinds=("tab",),
        builtin=True,
    ),
    Skill(
        id="summarize",
        name="Summarize",
        description="Summarize the current page, tab, or selected context.",
        instructions=(
            "Summarize my selected context (or the current tab, if nothing "
            "is selected). Give a short summary and the key points."),
        allowed_tools=READ_TOOLS,
        output_schema=_SUMMARIZE_OUTPUT_SCHEMA,
        default_context_kinds=("tab",),
        builtin=True,
    ),
    Skill(
        id="plan",
        name="Plan",
        description="Turn a goal into a Mission with a concrete plan.",
        instructions=(
            "Turn my request into a concrete plan: use mission_save_result or "
            "mission_set_progress to lay out the steps, and mission_save_"
            "constraints for anything I said must be true of the outcome. "
            "Check the current page or my selected context first if it is "
            "relevant to the plan."),
        allowed_tools=READ_TOOLS + MISSION_TOOLS,
        default_context_kinds=("mission",),
        builtin=True,
    ),
    Skill(
        id="audit",
        name="Audit",
        description="Review selected pages or files for problems and record "
                    "each one as a finding.",
        instructions=(
            "Audit my selected context (or the current tab, if nothing is "
            "selected) for problems, risks, or things worth flagging. Record "
            "each one with mission_save_finding, and note anything you "
            "checked but found no issue with using mission_note_source."),
        allowed_tools=READ_TOOLS + NAVIGATE_TOOLS + MISSION_TOOLS,
        default_context_kinds=("tab", "mission"),
        builtin=True,
    ),
)

_BUILTIN_BY_ID = {skill.id: skill for skill in BUILTIN_SKILLS}


def builtin_skill(skill_id: str) -> Skill | None:
    return _BUILTIN_BY_ID.get(skill_id)


class OutputValidationError(ValueError):
    """The model's answer did not match the Skill's output schema. Always
    carries a message safe to show a person - never a parser traceback."""


def _extract_json(text: str) -> object:
    """The model's answer, parsed as JSON.

    Tries the whole message first (the common case, since the Skill's
    instructions ask for "a single JSON object and nothing else"), then
    falls back to the first ``{...}`` span in case the model added a
    sentence around it - a best-effort recovery, not a guarantee, which is
    the honest amount of "structured output support" a prompt-only
    approach can offer.
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            pass
    raise OutputValidationError("The answer was not valid JSON.")


def _validate_against_schema(value: object, schema: dict, *, path: str = "") -> None:
    """A minimal, dependency-free JSON Schema subset: type/required/
    properties/items/enum. Enough for the built-in Skills' own schemas and
    anything a custom Skill is likely to need; not a full JSON Schema
    implementation, and does not pretend to be one."""
    label = path or "value"
    expected_type = schema.get("type")
    type_map = {"object": dict, "array": list, "string": str,
               "number": (int, float), "integer": int, "boolean": bool}
    if expected_type in type_map and not isinstance(value, type_map[expected_type]):
        raise OutputValidationError(f"'{label}' should be a {expected_type}.")
    if "enum" in schema and value not in schema["enum"]:
        raise OutputValidationError(f"'{label}' must be one of {schema['enum']}.")
    if expected_type == "object" and isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise OutputValidationError(f"'{label}' is missing required field '{key}'.")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value:
                _validate_against_schema(value[key], subschema, path=f"{label}.{key}")
    if expected_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_against_schema(item, item_schema, path=f"{label}[{index}]")


def validate_output(schema: dict, text: str) -> dict:
    """Parse and validate ``text`` against ``schema``. Raises
    OutputValidationError, with a message safe to show a person, on
    anything malformed - never raises a bare parser/KeyError."""
    value = _extract_json(text)
    _validate_against_schema(value, schema)
    return value if isinstance(value, dict) else {"value": value}
