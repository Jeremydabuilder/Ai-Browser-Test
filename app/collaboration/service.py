"""Phase 21 Collaboration / Shared Missions - the one thing UI code
constructs and holds. Builds entirely on Phase 20's primitives: the same
stable global ids (GlobalIdStore), the same conflict policy/engine
(app.sync.engine.SyncEngine, app.sync.conflicts), the same encryption
primitives (app.sync.crypto), and the same device identity
(app.sync.device) - never a second Mission model, a second crypto scheme,
or a second sync engine implementation (see the module docstrings this
one leans on for why).

What is genuinely new here: a separate, per-Mission collaboration key
(never the personal device's sync master key - Part 4), an encrypted
invite blob as the join mechanism (Part 3 - no PyBrowser identity
service), and the participant/role/comment/activity data Phase 20 has no
concept of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agent.keys import ApiKeyStore, KeyringUnavailable
from app.collaboration.adapters import ActivityAdapter, CommentAdapter, ParticipantAdapter
from app.collaboration.types import ActivityKind, Role, TargetType, can_comment
from app.sync import crypto
from app.sync.adapters import MissionAdapter, MissionFindingAdapter
from app.sync.device import ensure_local_device
from app.sync.engine import SyncEngine, SyncResult, SyncStatus
from app.sync.providers.local_folder import LocalFolderProvider
from app.sync.types import RecordType

if TYPE_CHECKING:
    from app.missions.repository import MissionStore
    from app.storage.collaboration_store import (
        ActivityStore, CommentStore, MissionSharingStore, ParticipantStore,
    )
    from app.storage.database import Database
    from app.storage.settings import SettingsStore
    from app.storage.sync_store import ConflictStore, GlobalIdStore, SyncVersionStore, TombstoneStore

_INVITE_CONTEXT = b"pybrowser-mission-invite"


class CollaborationError(Exception):
    """A collaboration action was refused - wrong invite passphrase, no
    permission for the requesting role, or the Mission is not shared."""


@dataclass(frozen=True)
class Invite:
    """What ``create_invite`` hands back: the encrypted bytes to send the
    invitee (by any channel PyBrowser is not involved in - AirDrop, email,
    a chat app) plus the passphrase, shown once, the same "share this out
    of band" shape Phase 20's recovery key already uses."""

    data: bytes
    passphrase: str


class CollaborationService:
    def __init__(self, db: "Database", settings: "SettingsStore", mission_store: "MissionStore",
                *, key_store_factory=None, graph: "object | None" = None) -> None:
        from app.storage.collaboration_store import (
            ActivityStore, CommentStore, MissionSharingStore, ParticipantStore,
        )
        from app.storage.sync_store import ConflictStore, GlobalIdStore, SyncDeviceStore, SyncVersionStore, TombstoneStore

        self._db = db
        self._settings = settings
        self._missions = mission_store
        #: Phase 21 Part 9: optional - when given, findings that arrive
        #: via collaboration sync also enter the local Knowledge Graph,
        #: tagged Provenance.COLLABORATOR_CONTENT (see
        #: app.sync.adapters.MissionFindingAdapter). None is a fully
        #: supported, honest configuration: sync still works, the
        #: Knowledge Graph is simply not updated from peer findings.
        self._graph = graph
        self.sharing: "MissionSharingStore" = MissionSharingStore(db)
        self.participants: "ParticipantStore" = ParticipantStore(db)
        self.comments: "CommentStore" = CommentStore(db)
        self.activity: "ActivityStore" = ActivityStore(db)
        #: The SAME global-id/version/tombstone/conflict tables Phase 20's
        #: personal sync uses - a Mission shared via collaboration and
        #: also carried by whole-profile sync converges on one identity
        #: and one conflict history, never two.
        self.global_ids: "GlobalIdStore" = GlobalIdStore(db)
        self.versions: "SyncVersionStore" = SyncVersionStore(db)
        self.tombstones: "TombstoneStore" = TombstoneStore(db)
        self.conflicts: "ConflictStore" = ConflictStore(db)
        #: Same device identity as Phase 20 - one identity for the whole
        #: app, reused here as the author of comments/activity/participant
        #: rows, rather than inventing a second one.
        self.device = ensure_local_device(settings, SyncDeviceStore(db))
        #: Overridable for tests - avoids every test needing a real OS
        #: keyring for a per-Mission key.
        self._key_store_factory = key_store_factory or (
            lambda global_id: ApiKeyStore(account=f"mission-key-{global_id}"))

    # -- key storage (Part 4: never the device's own sync master key) ------
    def _key_store(self, mission_global_id: str) -> ApiKeyStore:
        return self._key_store_factory(mission_global_id)

    def _store_key(self, mission_global_id: str, key: bytes) -> None:
        self._key_store(mission_global_id).set_key(key.hex())

    def _load_key(self, mission_global_id: str) -> bytes:
        try:
            raw = self._key_store(mission_global_id).get_keyring_key()
        except KeyringUnavailable as exc:
            raise CollaborationError(f"OS keyring unavailable: {exc}") from exc
        if not raw:
            raise CollaborationError("No collaboration key stored for this Mission.")
        return bytes.fromhex(raw)

    # -- roles/permissions ---------------------------------------------------
    def role_for(self, mission_id: int) -> str:
        """This device's role in ``mission_id`` - Role.OWNER if the
        Mission is not shared at all (today's ordinary, unshared-Mission
        behavior: full local control, unchanged)."""
        sharing = self.sharing.get(mission_id)
        if sharing is None or not sharing.is_active:
            return Role.OWNER
        participant = self.participants.get(mission_id, self.device.id)
        if participant is None or not participant.is_active:
            return Role.OWNER if sharing.owner_device_id == self.device.id else Role.VIEWER
        return participant.role

    def is_shared(self, mission_id: int) -> bool:
        sharing = self.sharing.get(mission_id)
        return sharing is not None and sharing.is_active

    # -- sharing / invites (Part 3/4/5) --------------------------------------
    def share_mission(self, mission_id: int, folder_path: str, *,
                      viewers_may_comment: bool = False) -> str:
        """Start sharing - generates a fresh, per-Mission collaboration
        key (never this device's sync master key), registers this device
        as Owner, and returns the Mission's stable global id."""
        mission_global_id = self.global_ids.ensure_global_id(RecordType.MISSION, str(mission_id))
        collaboration_key = crypto.generate_master_key()
        self._store_key(mission_global_id, collaboration_key)
        self.sharing.start(mission_id, mission_global_id, self.device.id, folder_path,
                           viewers_may_comment=viewers_may_comment)
        self.participants.add(mission_id, self.device.id, self.device.name, Role.OWNER)
        self._add_activity(mission_id, ActivityKind.MISSION_SHARED,
                           f"{self.device.name} shared this Mission")
        return mission_global_id

    def create_invite(self, mission_id: int, *, role: str = Role.EDITOR,
                      passphrase: str | None = None) -> Invite:
        """Part 3: the encrypted invite file/blob mechanism - no PyBrowser
        identity service, no account. The recipient needs this blob, the
        passphrase, and access to the same shared folder (Part 5:
        iCloud/Dropbox/OneDrive or any folder both people can reach)."""
        if role not in Role.ALL or role == Role.OWNER:
            raise CollaborationError(f"Cannot invite as role '{role}'.")
        if not self._is_owner(mission_id):
            raise CollaborationError("Only the owner can invite participants.")
        sharing = self.sharing.get(mission_id)
        if sharing is None or not sharing.is_active:
            raise CollaborationError("This Mission is not shared.")
        mission = self._missions.get(mission_id, with_pages=False)
        key = self._load_key(sharing.global_id)
        passphrase = passphrase or crypto.generate_recovery_key()
        payload = {
            "mission_global_id": sharing.global_id,
            "mission_title": mission.title if mission is not None else "Shared Mission",
            "mission_goal": mission.goal if mission is not None else "Shared Mission",
            "collaboration_key": key.hex(), "role": role,
            "owner_device_id": sharing.owner_device_id, "folder_path": sharing.folder_path,
            "viewers_may_comment": sharing.viewers_may_comment,
        }
        wrapped = crypto.wrap_key(json.dumps(payload).encode("utf-8"), passphrase,
                                  context=_INVITE_CONTEXT)
        return Invite(data=wrapped.to_bytes(), passphrase=passphrase)

    def join_mission(self, invite_data: bytes, passphrase: str) -> int:
        """Part 3's other half - a brand-new device (or one that already
        has this Mission locally, e.g. via whole-profile sync) processes
        an invite blob. Raises CollaborationError (wrapping CryptoError)
        for a wrong passphrase - never partially joins."""
        try:
            wrapped = crypto.WrappedKey.from_bytes(invite_data)
            raw = crypto.unwrap_key(wrapped, passphrase, context=_INVITE_CONTEXT)
            payload = json.loads(raw.decode("utf-8"))
        except (crypto.CryptoError, ValueError) as exc:
            raise CollaborationError("Could not open this invite - check the passphrase.") from exc

        mission_global_id = payload["mission_global_id"]
        collaboration_key = bytes.fromhex(payload["collaboration_key"])
        folder_path = payload.get("folder_path", "")

        existing_local_id = self.global_ids.get_local_id(RecordType.MISSION, mission_global_id)
        if existing_local_id is not None:
            mission_id = int(existing_local_id)
        else:
            # Materialize the Mission from what is actually in the shared
            # folder, rather than creating an independent local copy from
            # the invite's snapshot - an identical-content Mission created
            # on both ends before either has ever synced would otherwise
            # look like a same-time concurrent edit to the conflict engine
            # (both sides "changed" relative to no prior agreed version)
            # and get flagged as a conflict for no real reason.
            bootstrap = SyncEngine(
                provider=LocalFolderProvider(folder_path), master_key=collaboration_key,
                device_id=self.device.id, adapters={RecordType.MISSION: MissionAdapter(self._missions, share_workspace=False)},
                global_ids=self.global_ids, versions=self.versions, tombstones=self.tombstones,
                conflicts=self.conflicts)
            bootstrap.sync_once()
            existing_local_id = self.global_ids.get_local_id(RecordType.MISSION, mission_global_id)
            if existing_local_id is None:
                raise CollaborationError("Could not find the shared Mission in that folder.")
            mission_id = int(existing_local_id)

        self._store_key(mission_global_id, collaboration_key)
        self.sharing.start(mission_id, mission_global_id, payload.get("owner_device_id", ""),
                           folder_path,
                           viewers_may_comment=bool(payload.get("viewers_may_comment", False)))
        self.participants.add(mission_id, self.device.id, self.device.name,
                              payload.get("role", Role.EDITOR))
        owner_device_id = payload.get("owner_device_id", "")
        if owner_device_id and self.participants.get(mission_id, owner_device_id) is None:
            self.participants.add(mission_id, owner_device_id, "Owner", Role.OWNER)
        self._add_activity(mission_id, ActivityKind.PARTICIPANT_JOINED,
                           f"{self.device.name} joined this Mission")
        return mission_id

    def _is_owner(self, mission_id: int) -> bool:
        return self.role_for(mission_id) == Role.OWNER

    # -- participants (Part 2/14/17) ------------------------------------
    def remove_participant(self, mission_id: int, device_id: str, *, rotate_key: bool = True) -> None:
        if not self._is_owner(mission_id):
            raise CollaborationError("Only the owner can remove participants.")
        self.participants.remove(mission_id, device_id)
        self._add_activity(mission_id, ActivityKind.PARTICIPANT_REMOVED,
                           f"A participant was removed from this Mission")
        if rotate_key:
            self.rotate_key(mission_id)

    def rotate_key(self, mission_id: int) -> bytes:
        """Part 17: generate a new collaboration key for *future*
        versions of the Mission. Deliberately does not - and cannot -
        retroactively revoke a removed participant's already-downloaded
        ciphertext or any content they already decrypted; distributing
        the new key to remaining participants is a fresh create_invite()
        per participant, same as the original invite (no in-band re-
        keying channel exists without a real identity service)."""
        if not self._is_owner(mission_id):
            raise CollaborationError("Only the owner can rotate the collaboration key.")
        sharing = self.sharing.get(mission_id)
        if sharing is None:
            raise CollaborationError("This Mission is not shared.")
        new_key = crypto.generate_master_key()
        self._store_key(sharing.global_id, new_key)
        return new_key

    def set_role(self, mission_id: int, device_id: str, role: str) -> None:
        if not self._is_owner(mission_id):
            raise CollaborationError("Only the owner can change roles.")
        if role not in Role.ALL:
            raise CollaborationError(f"Unknown role '{role}'.")
        self.participants.set_role(mission_id, device_id, role)

    def stop_sharing(self, mission_id: int) -> None:
        if not self._is_owner(mission_id):
            raise CollaborationError("Only the owner can stop sharing.")
        self.sharing.stop(mission_id)

    # -- comments (Part 2/6/13) -----------------------------------------
    def add_comment(self, mission_id: int, target_type: str, target_id: str, body: str):
        if target_type not in TargetType.ALL:
            raise CollaborationError(f"Unknown comment target '{target_type}'.")
        body = (body or "").strip()
        if not body:
            raise CollaborationError("A comment needs some text.")
        sharing = self.sharing.get(mission_id)
        viewers_may_comment = sharing.viewers_may_comment if sharing is not None else True
        if not can_comment(self.role_for(mission_id), viewers_may_comment=viewers_may_comment):
            raise CollaborationError("You do not have permission to comment on this Mission.")
        comment = self.comments.add(mission_id, target_type, target_id, self.device.id,
                                    self.device.name, body)
        self._add_activity(mission_id, ActivityKind.COMMENT_ADDED,
                           f"{self.device.name} commented")
        return comment

    def comments_for(self, mission_id: int, target_type: str | None = None,
                     target_id: str | None = None):
        if target_type is not None and target_id is not None:
            return self.comments.for_target(mission_id, target_type, target_id)
        return self.comments.for_mission(mission_id)

    # -- activity feed (Part 7) ---------------------------------------------
    def _add_activity(self, mission_id: int, kind: str, summary: str) -> None:
        self.activity.add(mission_id, kind, self.device.id, self.device.name, summary)

    def activity_feed(self, mission_id: int, *, limit: int = 50):
        return self.activity.for_mission(mission_id, limit=limit)

    # -- sync (Part 5/8/16) ---------------------------------------------
    def sync_now(self, mission_id: int) -> SyncResult:
        """Runs a SyncEngine scoped to exactly this one Mission - its own
        provider (the shared folder), its own key (never the personal
        sync master key), but the SAME conflict policy/global-id/version/
        tombstone machinery Phase 20 already built. Offline is a normal
        result (Part 16), not an error - see SyncStatus.OFFLINE."""
        sharing = self.sharing.get(mission_id)
        if sharing is None or not sharing.is_active:
            return SyncResult(status=SyncStatus.OFF)
        try:
            key = self._load_key(sharing.global_id)
        except CollaborationError as exc:
            return SyncResult(status=SyncStatus.ERROR, error=str(exc))

        before_finding_ids = {f.id for f in self._missions.findings(mission_id)}
        before_comment_ids = {c.id for c in self.comments.for_mission(mission_id)}

        provider = LocalFolderProvider(sharing.folder_path)
        mission_ids = {mission_id}
        adapters = {
            RecordType.MISSION: MissionAdapter(self._missions, mission_ids=mission_ids, share_workspace=False),
            RecordType.MISSION_FINDING: MissionFindingAdapter(
                self._missions, self.global_ids, mission_ids=mission_ids,
                graph=self._graph, origin_device_id=self.device.id),
            RecordType.MISSION_COMMENT: CommentAdapter(self.comments, mission_id),
            RecordType.MISSION_ACTIVITY: ActivityAdapter(self.activity, mission_id),
            RecordType.MISSION_PARTICIPANT: ParticipantAdapter(self.participants, mission_id),
        }

        def _persist_token(token: str) -> None:
            self.sharing.set_since_token(mission_id, token)

        engine = SyncEngine(
            provider=provider, master_key=key, device_id=self.device.id, adapters=adapters,
            global_ids=self.global_ids, versions=self.versions, tombstones=self.tombstones,
            conflicts=self.conflicts, since_token=sharing.since_token,
            on_since_token=_persist_token)
        result = engine.sync_once()

        if result.status != SyncStatus.OFFLINE:
            after_finding_ids = {f.id for f in self._missions.findings(mission_id)}
            for _new_id in after_finding_ids - before_finding_ids:
                self._add_activity(mission_id, ActivityKind.FINDING_ADDED,
                                   "A finding was added")
            after_comments = {c.id: c for c in self.comments.for_mission(mission_id)}
            for new_id, comment in after_comments.items():
                if new_id in before_comment_ids:
                    continue
                if comment.author_device_id == self.device.id:
                    continue  # already announced locally by add_comment()
                self._add_activity(mission_id, ActivityKind.COMMENT_ADDED,
                                   f"{comment.author_name or 'A collaborator'} commented")
        return result

    # -- export (Part 18) ----------------------------------------------
    def export_markdown(self, mission_id: int) -> str:
        """Part 18: goal/findings/sources/result/contributors only -
        never private device data (this device's own settings, other
        Missions, anything not part of this one shared Mission)."""
        mission = self._missions.get(mission_id, with_pages=False)
        if mission is None:
            raise CollaborationError("Mission not found.")
        findings = self._missions.findings(mission_id)
        participants = [p for p in self.participants.all(mission_id) if p.is_active]
        lines = [f"# {mission.title}", "", "## Goal", mission.goal, ""]
        if mission.constraints:
            lines += ["## Constraints", *(f"- {c}" for c in mission.constraints), ""]
        lines.append("## Findings")
        for finding in findings:
            source = f" ({finding.source_url})" if finding.source_url else ""
            lines.append(f"- {finding.text}{source}")
        lines.append("")
        if mission.result:
            lines += ["## Result", mission.result, ""]
        if participants:
            lines.append("## Contributors")
            lines += [f"- {p.display_name or p.device_id} ({p.role})" for p in participants]
        return "\n".join(lines)
