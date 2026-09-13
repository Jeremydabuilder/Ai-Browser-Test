"""The recorded-workflow data model: what a Skill's optional ``workflow``
field actually holds.

Deliberately *not* a new top-level entity - Phase 16's own brief asks for
"Skill + optional structured workflow steps" rather than a second, unrelated
system, so a RecordedWorkflow only ever exists attached to a Skill (see
app/agent/skills.py's Skill.workflow) and is persisted as one JSON column
next to the Skill's own row (app/storage/skills.py).

Design choices worth calling out:

* **Semantic targets, not coordinates.** Each step that acts on the page
  carries a SemanticTarget (role/accessible name/visible text/field name),
  the same vocabulary app/browser/controller.py's find_elements() and
  get_page_structure() already speak. Replay re-resolves this fresh every
  time (see app/automation/runner.py) rather than trusting the ``ref`` that
  was current at recording time - refs are a snapshot-scoped id and go stale
  the moment the page changes, which is exactly the "never replay stale
  targets" rule this phase's brief insists on. A ref is therefore never
  stored on a RecordedStep at all, only in the transient recording buffer
  (see app/automation/recorder.py) - persisting one would invite exactly the
  bug this model is designed to make impossible.
* **Parameters are placeholders inside args, not a side table.** A recorded
  ``{"text": "tennis shoes"}`` becomes ``{"text": "{{query}}"}`` plus one
  WorkflowParameter named "query" - mirroring the brief's own worked example
  verbatim. render_args() below is the only place that ever expands one back
  into a real value.
* **Secrets are placeholders too, but never become parameters.** A field
  app.browser.safety.classify_type() calls SENSITIVE is recorded as
  ``{{credential:<hint>}}`` (see recorder.py) and is filtered out of
  parameter_candidates() - a workflow must never *offer* a secret as
  something to fill in from a plain text box.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

#: Bumped when this shape changes in a way older stored JSON cannot just be
#: read as (new required field, renamed key, etc.) - see Skill.version's
#: identical role for the rest of the Skill row.
WORKFLOW_SCHEMA_VERSION = 1

MAX_STEPS = 60

#: ``{{name}}`` - an ordinary parameter. ``{{credential:hint}}`` - a secret,
#: resolved at replay time through secure credential storage, never through
#: a plain text parameter box. Names are restricted to a plain identifier
#: shape so a malicious page value like ``{{__import__('os')}}`` is just an
#: inert literal string, never something a later templating step evaluates.
PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
CREDENTIAL_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*credential:([A-Za-z0-9_.\-]+)\s*\}\}")
#: Back-compat aliases for in-package use.
_PLACEHOLDER = PLACEHOLDER_PATTERN
_CREDENTIAL_PLACEHOLDER = CREDENTIAL_PLACEHOLDER_PATTERN


def credential_placeholder(hint: str) -> str:
    return f"{{{{credential:{hint}}}}}"


def parameter_placeholder(name: str) -> str:
    return f"{{{{{name}}}}}"


@dataclass(frozen=True)
class SemanticTarget:
    """A page element, described the way a person would point it out - never
    by (x, y). Every field is optional because not every page names every
    control well; resolve_target() in runner.py degrades through whichever
    fields are actually present, most specific first."""

    role: str = ""
    name: str = ""          # accessible name / label
    text: str = ""           # visible text, for elements with no name
    field_name: str = ""     # form field name/id, when the page provides one
    placeholder: str = ""    # input placeholder text
    frame_url: str = ""      # "" = main document

    def queries(self) -> list[str]:
        """Search strings for find_elements(), most specific first."""
        seen: list[str] = []
        for value in (self.name, self.field_name, self.placeholder, self.text):
            value = (value or "").strip()
            if value and value not in seen:
                seen.append(value)
        return seen

    def is_empty(self) -> bool:
        return not any((self.role, self.name, self.text, self.field_name, self.placeholder))

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "text": self.text,
                "field_name": self.field_name, "placeholder": self.placeholder,
                "frame_url": self.frame_url}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SemanticTarget | None":
        if not data:
            return None
        return cls(role=data.get("role", ""), name=data.get("name", ""),
                   text=data.get("text", ""), field_name=data.get("field_name", ""),
                   placeholder=data.get("placeholder", ""), frame_url=data.get("frame_url", ""))


@dataclass(frozen=True)
class McpStepInfo:
    """Recorded metadata for a step that calls an MCP tool - see the Phase 16
    brief's "MCP RECORDING" section. schema_fingerprint is compared again at
    replay (see runner.py); a mismatch always pauses for review, never
    silently proceeds against a tool whose shape has changed since."""

    server_id: str
    tool_name: str
    schema_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {"server_id": self.server_id, "tool_name": self.tool_name,
                "schema_fingerprint": self.schema_fingerprint}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "McpStepInfo | None":
        if not data:
            return None
        return cls(server_id=data.get("server_id", ""), tool_name=data.get("tool_name", ""),
                   schema_fingerprint=data.get("schema_fingerprint", ""))


@dataclass(frozen=True)
class RecordedStep:
    id: int
    tool_name: str
    #: Recorded arguments. A value may contain a ``{{param}}`` or
    #: ``{{credential:x}}`` placeholder - see render_args(). Never contains a
    #: ``ref`` key: see the module docstring on why.
    args: dict[str, Any] = field(default_factory=dict)
    target: SemanticTarget | None = None
    requires_visual_fallback: bool = False
    #: Non-authoritative hint only, e.g. {"x": 110, "y": 160} - consulted by
    #: the visual fallback path in runner.py only after structured resolution
    #: has already failed, never as a first resort.
    visual_hint: dict[str, Any] | None = None
    #: Optional post-condition, checked after the step runs. Recognised keys:
    #: "url_contains", "heading_contains", "element_text_query" (an element
    #: matching this query must exist), "element_gone_query" (must not).
    expect: dict[str, Any] | None = None
    mcp: McpStepInfo | None = None
    optional: bool = False
    #: Human-readable summary shown in the Finish/editor UI, e.g.
    #: "Type '{{query}}' into Search". Purely descriptive - replay never
    #: parses this, only args/target/expect.
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tool_name": self.tool_name, "args": dict(self.args),
            "target": self.target.as_dict() if self.target else None,
            "requires_visual_fallback": self.requires_visual_fallback,
            "visual_hint": self.visual_hint,
            "expect": self.expect,
            "mcp": self.mcp.as_dict() if self.mcp else None,
            "optional": self.optional,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordedStep":
        return cls(
            id=int(data.get("id", 0)), tool_name=data.get("tool_name", ""),
            args=dict(data.get("args") or {}),
            target=SemanticTarget.from_dict(data.get("target")),
            requires_visual_fallback=bool(data.get("requires_visual_fallback", False)),
            visual_hint=data.get("visual_hint"),
            expect=data.get("expect"),
            mcp=McpStepInfo.from_dict(data.get("mcp")),
            optional=bool(data.get("optional", False)),
            label=data.get("label", ""),
        )

    def placeholder_names(self) -> set[str]:
        found: set[str] = set()
        for value in self.args.values():
            if isinstance(value, str):
                found.update(_PLACEHOLDER.findall(value))
        # Credential placeholders share the same {{...}} syntax but are never
        # ordinary parameters - drop anything that is actually a credential.
        return {name for name in found if not name.startswith("credential:")}

    def credential_hints(self) -> set[str]:
        found: set[str] = set()
        for value in self.args.values():
            if isinstance(value, str):
                found.update(_CREDENTIAL_PLACEHOLDER.findall(value))
        return found


@dataclass(frozen=True)
class WorkflowParameter:
    name: str
    label: str = ""
    default: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "default": self.default}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowParameter":
        return cls(name=data.get("name", ""), label=data.get("label", ""),
                   default=data.get("default", ""))


@dataclass(frozen=True)
class RecordedWorkflow:
    """The optional structured-automation half of a Skill."""

    steps: tuple[RecordedStep, ...] = field(default_factory=tuple)
    parameters: tuple[WorkflowParameter, ...] = field(default_factory=tuple)
    #: Bumped by bump_version() every time an already-saved workflow is
    #: edited - see the brief's VERSIONING section. Never full history, just
    #: "has this changed since it was recorded" for stale-recording detection.
    version: int = 1
    #: Recording provenance - purely informational, never consulted by
    #: replay logic, so a corrupt/missing value here can never change what a
    #: workflow does.
    recorded_at: float = 0.0
    recorder_version: int = WORKFLOW_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.as_dict() for s in self.steps],
            "parameters": [p.as_dict() for p in self.parameters],
            "version": self.version,
            "recorded_at": self.recorded_at,
            "recorder_version": self.recorder_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RecordedWorkflow | None":
        if not data:
            return None
        return cls(
            steps=tuple(RecordedStep.from_dict(s) for s in data.get("steps") or []),
            parameters=tuple(WorkflowParameter.from_dict(p) for p in data.get("parameters") or []),
            version=int(data.get("version", 1)),
            recorded_at=float(data.get("recorded_at", 0.0)),
            recorder_version=int(data.get("recorder_version", WORKFLOW_SCHEMA_VERSION)),
        )

    def bump_version(self) -> "RecordedWorkflow":
        from dataclasses import replace
        return replace(self, version=self.version + 1)

    def parameter_names(self) -> set[str]:
        names: set[str] = set()
        for step in self.steps:
            names.update(step.placeholder_names())
        return names


def new_workflow(steps: list[RecordedStep], parameters: list[WorkflowParameter] | None = None
                 ) -> RecordedWorkflow:
    return RecordedWorkflow(steps=tuple(steps[:MAX_STEPS]), parameters=tuple(parameters or ()),
                            version=1, recorded_at=time.time())


def render_args(args: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Substitute ``{{param}}`` placeholders with caller-supplied values.

    Never touches a ``{{credential:...}}`` placeholder - that is resolved
    separately, through secure credential storage, by the caller *before* or
    *after* this (see runner.py) so a plain parameter dict passed around in
    memory never needs to carry a real secret through this function.
    """

    def substitute(value: Any) -> Any:
        if not isinstance(value, str):
            return value

        def repl(match: "re.Match[str]") -> str:
            name = match.group(1)
            return values.get(name, match.group(0))

        return _PLACEHOLDER.sub(repl, value)

    return {key: substitute(value) for key, value in args.items()}


def parameter_candidates(steps: list[RecordedStep]) -> list[WorkflowParameter]:
    """Parameters worth offering after a recording finishes - one entry per
    distinct placeholder name actually present in the recorded steps, never
    including a credential placeholder (see RecordedStep.placeholder_names)."""
    seen: dict[str, WorkflowParameter] = {}
    for step in steps:
        for name in step.placeholder_names():
            if name not in seen:
                seen[name] = WorkflowParameter(name=name, label=name.replace("_", " ").title())
    return list(seen.values())
