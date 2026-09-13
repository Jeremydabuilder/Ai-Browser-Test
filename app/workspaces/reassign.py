"""What happens to a workspace's data when the workspace itself is deleted.

Never silent, and never a cascade delete: a Mission, a scheduled task, and
indexed knowledge are all real records someone may care about long after
the Workspace that first held them is gone. Deleting the Workspace row
itself (see WorkspaceStore.delete) only ever happens after its data has
been explicitly reassigned - moved to another Workspace, or made global
(workspace_id NULL) - by a caller that asked the user first (see
app/ui/workspace_switcher.py's DeleteWorkspaceDialog).

Tab/session state is the one exception: a closed workspace's open tabs are
not a durable record of anything, and are simply not carried anywhere -
deleting the Workspace row (which is where TabState itself lives) is
already "deleting" them.
"""

from __future__ import annotations

from app.storage.database import Database


def reassign_workspace_data(db: Database, *, from_workspace_id: str,
                            to_workspace_id: str | None) -> None:
    """Move every Mission, scheduled task, Watch, and indexed knowledge
    chunk scoped to ``from_workspace_id`` to ``to_workspace_id`` (or to the
    global/unscoped state, NULL, if ``to_workspace_id`` is None).

    Recorded-workflow Skills are handled separately by the caller (see
    reassign_recorded_workflows) since a Skill's binding is informational
    only and Skills are not otherwise workspace-scoped.
    """
    for table in ("missions", "scheduled_tasks", "watches", "knowledge_chunks"):
        db.execute(f"UPDATE {table} SET workspace_id = ? WHERE workspace_id = ?",
                  (to_workspace_id, from_workspace_id))


def reassign_recorded_workflows(db: Database, *, from_workspace_id: str,
                                to_workspace_id: str | None) -> None:
    """A recorded workflow only remembers its workspace as a fact ("taught
    here") - not a hard binding - so on deletion it simply forgets it
    rather than being reassigned to look like it was taught somewhere it
    was not. Passing the same ``to_workspace_id`` a Mission move used is
    for the one case that makes sense to actually update: moving to a
    surviving Workspace, if that's clearly what happened. Deleting to
    "global" clears it instead."""
    if to_workspace_id is not None:
        db.execute(
            "UPDATE skills SET workflow_workspace_id = ? WHERE workflow_workspace_id = ?",
            (to_workspace_id, from_workspace_id))
    else:
        db.execute(
            "UPDATE skills SET workflow_workspace_id = NULL WHERE workflow_workspace_id = ?",
            (from_workspace_id,))
