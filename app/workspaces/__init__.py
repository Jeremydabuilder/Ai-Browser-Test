"""Phase 17: Browser Workspaces / Profiles.

A Workspace is a scoping layer around infrastructure that already exists -
BrowserController, TabManager, Missions, Skills, MCP, Settings, session
persistence - never a second implementation of any of them. See
app/workspaces/model.py for the data shape and the module docstring there
for the Workspace/Isolated Profile distinction this phase is careful to
keep honest.
"""

from app.workspaces.model import TabRecord, TabState, Workspace
from app.workspaces.store import WorkspaceStore

__all__ = ["Workspace", "TabState", "TabRecord", "WorkspaceStore"]
