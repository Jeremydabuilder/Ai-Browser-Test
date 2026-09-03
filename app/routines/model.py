"""What a Routine is: a taught sequence of browser actions.

Other AI browsers let you tell the agent what to do. PyBrowser also lets you
show it: perform a task once through Py, save the sequence, and run it again
later with different inputs.

**Scope, stated plainly.** A Routine records the agent's own tool calls while
"Teach Py" is active - not raw mouse clicks in the page. Manual browsing never
goes through BrowserController (see app/browser/controller.py), so there is
nowhere today to observe a click the user made by hand. Teaching Py means
directing it through chat while recording is on; what gets saved is the
semantic actions it took (navigate, click a named element, type into a named
field), not screen coordinates - so a saved Routine still works if a page's
layout changes, the way a coordinate-based macro would not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A Routine step's arguments are capped in count and in size, the same
#: reasoning as everywhere else data crosses from the agent into storage:
#: refused, not silently trimmed, if a single argument is unreasonable.
MAX_STEPS = 40
MAX_ARG_CHARS = 4000
MAX_NAME_CHARS = 100

#: Argument keys never offered as variables when a Routine is run. They are
#: identifiers into a specific page snapshot or a specific open tab, not
#: something a person meant to vary - "ref" from a page that no longer exists
#: is not a rerunnable input, it is a stale coordinate.
NON_VARIABLE_KEYS = {"ref", "tab_id", "snapshot_id"}


def collapse(text: str) -> str:
    return " ".join((text or "").split())


@dataclass(frozen=True)
class RoutineStep:
    """One recorded action: a tool name and the arguments it was called with."""

    id: int
    routine_id: int
    position: int
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    #: What the step looked like when recorded, for a human reading the list -
    #: the same sentence the step checklist would have shown.
    description: str = ""

    def variable_keys(self) -> list[str]:
        """Which of this step's arguments are worth letting the user change."""
        return [key for key, value in self.args.items()
                if key not in NON_VARIABLE_KEYS and isinstance(value, str)]

    def slot(self, key: str) -> str:
        """A stable id for one editable argument, unique within the Routine."""
        return f"s{self.position}.{key}"


@dataclass(frozen=True)
class Routine:
    """A named, saved sequence of steps, taught once and replayed on request."""

    id: int
    mission_id: int
    name: str
    created_at: str = ""
    updated_at: str = ""
    steps: tuple[RoutineStep, ...] = field(default_factory=tuple)

    def resolve(self, overrides: dict[str, str] | None = None
               ) -> list[tuple[str, dict[str, Any]]]:
        """The steps as (tool_name, args) pairs, with variable slots filled in.

        ``overrides`` keys are step.slot() ids. Anything not overridden keeps
        the value it was recorded with. This never invents a tool call and
        never adds an argument that was not there originally - it only ever
        substitutes a value already present.
        """
        overrides = overrides or {}
        resolved: list[tuple[str, dict[str, Any]]] = []
        for step in self.steps:
            args = dict(step.args)
            for key in step.variable_keys():
                slot = step.slot(key)
                if slot in overrides:
                    args[key] = overrides[slot]
            resolved.append((step.tool_name, args))
        return resolved
