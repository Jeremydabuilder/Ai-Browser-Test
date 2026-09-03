"""Recording and running Routines.

The only part of this that touches the browser is playback, and playback does
not touch the browser directly either: it hands a list of (tool_name, args)
pairs to AgentSession.run_routine, which runs them through the exact same
assess()/confirmation/execute path a model-issued tool call takes. A Routine
step that would need the user's approval still asks for it, every time it
runs - recording a sensitive action once does not make it unsupervised
forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.routines.model import Routine
from app.routines.repository import RoutineStore

#: Only the agent's own browsing actions are worth teaching back. Mission
#: bookkeeping tools (mission_save_finding and friends) are not steps in a web
#: workflow; recording them would replay Mission side effects, not the task.
RECORDABLE_PREFIX = "browser_"


@dataclass
class _Draft:
    mission_id: int
    steps: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)


class RoutineService:
    def __init__(self, store: RoutineStore) -> None:
        self._store = store
        self._draft: _Draft | None = None

    @property
    def is_recording(self) -> bool:
        return self._draft is not None

    @property
    def recorded_count(self) -> int:
        return len(self._draft.steps) if self._draft else 0

    # -- recording ---------------------------------------------------------
    def begin_recording(self, mission_id: int) -> bool:
        if self._draft is not None:
            return False
        self._draft = _Draft(mission_id=mission_id)
        return True

    def record_step(self, tool_name: str, args: dict[str, Any],
                    description: str = "") -> None:
        """Called by AgentSession after a tool succeeds. Ignored unless
        recording is on; browser_* only, see RECORDABLE_PREFIX."""
        if self._draft is None or not tool_name.startswith(RECORDABLE_PREFIX):
            return
        self._draft.steps.append((tool_name, dict(args), description))

    def discard_recording(self) -> None:
        self._draft = None

    def stop_recording(self, name: str) -> Routine | None:
        """Save what was recorded as a named Routine. Clears the draft either way."""
        draft = self._draft
        self._draft = None
        if draft is None or not draft.steps:
            return None
        return self._store.create(draft.mission_id, name, draft.steps)

    # -- running -------------------------------------------------------
    def for_mission(self, mission_id: int) -> list[Routine]:
        return self._store.for_mission(mission_id)

    def get(self, routine_id: int) -> Routine | None:
        return self._store.get(routine_id)

    def delete(self, routine_id: int) -> None:
        self._store.delete(routine_id)

    def rename(self, routine_id: int, name: str) -> bool:
        return self._store.rename(routine_id, name)
