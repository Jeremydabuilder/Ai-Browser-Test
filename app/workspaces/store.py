"""Persistence for Workspaces - see app/storage/database.py's ``workspaces``
table (migration 24) and app/workspaces/model.py for the shape stored here.
"""

from __future__ import annotations

import json

from app.storage.database import Database
from app.workspaces.model import DEFAULT_WORKSPACE_ID, Workspace, default_workspace

MAX_NAME_CHARS = 60


def _row_to_workspace(row) -> Workspace:
    return Workspace.from_dict(json.loads(row["data_json"]))


class WorkspaceStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self.ensure_default()

    def ensure_default(self) -> Workspace:
        """Every profile has at least one Workspace, created transparently
        the first time this store is used - a profile that pre-dates Phase
        17 gets exactly one Workspace ("Home") holding nothing but the
        implicit global scope it already had, so nothing about its
        Missions/tabs/Skills silently changes meaning."""
        existing = self.get(DEFAULT_WORKSPACE_ID)
        if existing is not None:
            return existing
        workspace = default_workspace()
        self.save(workspace)
        return workspace

    def all(self) -> list[Workspace]:
        rows = self._db.query("SELECT * FROM workspaces ORDER BY created_at ASC")
        return [_row_to_workspace(row) for row in rows]

    def get(self, workspace_id: str) -> Workspace | None:
        row = self._db.query_one("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        return _row_to_workspace(row) if row is not None else None

    def save(self, workspace: Workspace) -> Workspace:
        name = (workspace.name or "").strip()[:MAX_NAME_CHARS] or "Workspace"
        if name != workspace.name:
            from dataclasses import replace
            workspace = replace(workspace, name=name)
        self._db.execute(
            "INSERT INTO workspaces (id, name, created_at, last_used_at, data_json) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "last_used_at=excluded.last_used_at, data_json=excluded.data_json",
            (workspace.id, name, workspace.created_at, workspace.last_used_at,
             json.dumps(workspace.as_dict())))
        return workspace

    def delete(self, workspace_id: str) -> bool:
        """Refuses to delete the last remaining Workspace, and never
        deletes DEFAULT_WORKSPACE_ID while it is the only one - there must
        always be somewhere for global Missions/tabs to belong. The caller
        (MainWindow's deletion flow) is what actually decides what happens
        to the departing workspace's Missions/scheduled tasks/context
        first - this only ever removes the row once that is settled."""
        if workspace_id == DEFAULT_WORKSPACE_ID and len(self.all()) <= 1:
            return False
        if len(self.all()) <= 1:
            return False
        self._db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        return True
