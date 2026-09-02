"""Missions: goal-based browsing workspaces.

A Mission is what the user is trying to accomplish. Tabs come and go; the
Mission remembers the goal and the pages that served it, and survives a
restart.

Three layers, deliberately separate:

* :mod:`app.missions.model` - the data, and the two judgements that must not be
  scattered around the codebase: what counts as the same page, and what a goal
  should be called.
* :mod:`app.missions.repository` - SQLite. Knows no policy.
* :mod:`app.missions.service` - the live state and the association rules. This
  is the only part that knows about the browser.
"""

from app.missions.model import (
    Mission,
    MissionPage,
    MissionStatus,
    PageSource,
    is_associable,
    page_key,
    title_from_goal,
)
from app.missions.repository import MissionStore
from app.missions.service import MissionService

__all__ = [
    "Mission",
    "MissionPage",
    "MissionStatus",
    "MissionService",
    "MissionStore",
    "PageSource",
    "is_associable",
    "page_key",
    "title_from_goal",
]
