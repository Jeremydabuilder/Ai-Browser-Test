"""Phase 21 Collaboration / Shared Missions - the participant role model
and comment/activity target vocabulary. Deliberately small (Part 1: "Keep
the role model simple.").
"""

from __future__ import annotations


class Role:
    """Owner/Editor/Viewer, nothing fancier. These gate what the *local*
    UI offers to do, and what a remote change is treated as (a Viewer's
    device should never be sending Mission-goal edits) - not a
    cryptographic access-control system. There is no signature scheme or
    identity service in this phase (Part 3 explicitly rules one out), so
    a determined peer with the collaboration key could locally construct
    a message claiming any role; the trust boundary here is "who you
    chose to share an encrypted folder with," the same boundary email/
    Dropbox-shared-folder collaboration has always had. See the
    checkpoint report's Known Limitations for this stated plainly.
    """

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"

    ALL = frozenset({OWNER, EDITOR, VIEWER})


class TargetType:
    """What a Comment (Part 6) can attach to."""

    MISSION = "mission"
    FINDING = "finding"
    SOURCE = "source"
    RESULT = "result"

    ALL = frozenset({MISSION, FINDING, SOURCE, RESULT})


class ActivityKind:
    """Part 7's own examples, as stable strings - a UI label is built from
    these plus the actor's name, never stored as pre-formatted prose (so
    a future locale/wording change does not need to rewrite history)."""

    PARTICIPANT_JOINED = "participant_joined"
    FINDING_ADDED = "finding_added"
    SOURCE_ADDED = "source_added"
    RESULT_EDITED = "result_edited"
    COMMENT_ADDED = "comment_added"
    MISSION_SHARED = "mission_shared"
    PARTICIPANT_REMOVED = "participant_removed"


#: Part 2: what each role may do *locally*. Checked by
#: app.collaboration.service.CollaborationService before an action that
#: would produce a synced change - never a substitute for the normal tool/
#: action approval flow, which every action still goes through regardless
#: of collaboration role (Part 2's own "Do not let collaboration roles
#: bypass PyBrowser's normal tool/action approvals").
_CAN_EDIT_RESULT = frozenset({Role.OWNER, Role.EDITOR})
_CAN_ADD_FINDING = frozenset({Role.OWNER, Role.EDITOR})
_CAN_MANAGE_PARTICIPANTS = frozenset({Role.OWNER})
_CAN_STOP_SHARING = frozenset({Role.OWNER})


def can_edit_result(role: str) -> bool:
    return role in _CAN_EDIT_RESULT


def can_add_finding(role: str) -> bool:
    return role in _CAN_ADD_FINDING


def can_manage_participants(role: str) -> bool:
    return role in _CAN_MANAGE_PARTICIPANTS


def can_stop_sharing(role: str) -> bool:
    return role in _CAN_STOP_SHARING


def can_comment(role: str, *, viewers_may_comment: bool) -> bool:
    """Part 2: "Viewer: read only. Comment only if you intentionally
    choose to allow it" - an explicit per-Mission opt-in, not a role
    default."""
    if role in (Role.OWNER, Role.EDITOR):
        return True
    return role == Role.VIEWER and viewers_may_comment
