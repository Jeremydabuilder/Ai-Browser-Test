"""Turning literal recorded values into ``{{parameter}}`` placeholders.

This is a *suggestion* pass, run once when recording finishes and shown in
the Create Automation / Skill dialog (the brief's own FINISH FLOW) for the
user to accept, rename, or reject per-value - it never runs silently, and
it never touches a value the recorder already turned into a
``{{credential:...}}`` placeholder (see recorder.py's _secret_safe_value):
a secret is never offered as something to fill in from a plain parameter
box, worked example in the brief or not.
"""

from __future__ import annotations

import re

from app.automation.model import (
    CREDENTIAL_PLACEHOLDER_PATTERN,
    PLACEHOLDER_PATTERN,
    RecordedStep,
    RecordedWorkflow,
    WorkflowParameter,
    parameter_placeholder,
)

#: Only these tools' typed value is ever a parameterization candidate - a
#: value the user visibly typed, one field at a time. Not, e.g. a
#: browser_navigate url or an mcp.* argument: those are frequently
#: structural (an endpoint, an id) rather than "the thing that varies", and
#: guessing wrong there would silently change what a step targets, not just
#: what it types.
_PARAMETERIZABLE_TOOLS = ("browser_type",)
_ARG_KEY = "text"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "value"


def suggest_parameters(steps: list[RecordedStep]) -> tuple[list[RecordedStep], list[WorkflowParameter]]:
    """Return (new_steps, parameters): steps with each eligible literal typed
    value replaced by a fresh ``{{name}}`` placeholder, and the matching
    WorkflowParameter (default = the original literal value, so a workflow
    still "does the same thing" if the user runs it without changing
    anything - exactly the Phase 16 brief's own worked examples).

    A step whose value is already a placeholder (parameter or credential) is
    left completely alone - re-suggesting over an existing placeholder, or
    turning a credential placeholder into an ordinary parameter, would
    silently weaken what was already decided about it.
    """
    new_steps: list[RecordedStep] = []
    parameters: list[WorkflowParameter] = []
    used_names: set[str] = set()

    for step in steps:
        if step.tool_name not in _PARAMETERIZABLE_TOOLS or _ARG_KEY not in step.args:
            new_steps.append(step)
            continue
        value = step.args.get(_ARG_KEY)
        if not isinstance(value, str) or not value.strip():
            new_steps.append(step)
            continue
        if PLACEHOLDER_PATTERN.search(value) or CREDENTIAL_PLACEHOLDER_PATTERN.search(value):
            new_steps.append(step)  # already a parameter or a secret
            continue

        base = ""
        if step.target is not None:
            base = step.target.field_name or step.target.placeholder or step.target.name
        name = _slug(base) or "value"
        candidate, suffix = name, 2
        while candidate in used_names:
            candidate, suffix = f"{name}_{suffix}", suffix + 1
        used_names.add(candidate)

        label = (step.target.name if step.target and step.target.name else candidate.replace("_", " ").title())
        parameters.append(WorkflowParameter(name=candidate, label=label, default=value))

        new_args = dict(step.args)
        new_args[_ARG_KEY] = parameter_placeholder(candidate)
        from dataclasses import replace
        new_steps.append(replace(step, args=new_args))

    return new_steps, parameters


def apply_parameter_suggestions(workflow: RecordedWorkflow) -> RecordedWorkflow:
    """Convenience wrapper used by the Finish-flow UI: suggest parameters for
    every step of a freshly-recorded workflow and fold them into it."""
    from dataclasses import replace

    new_steps, suggested = suggest_parameters(list(workflow.steps))
    existing_names = {p.name for p in workflow.parameters}
    merged = list(workflow.parameters) + [p for p in suggested if p.name not in existing_names]
    return replace(workflow, steps=tuple(new_steps), parameters=tuple(merged))
