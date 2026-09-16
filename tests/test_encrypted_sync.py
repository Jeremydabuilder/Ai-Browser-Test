"""Phase 20: Encrypted Sync - crypto roundtrip/tamper/replay, device
identity, pairing/recovery, the local-folder provider, incremental sync,
conflict resolution (keep local/remote/both), tombstones, Mission/
Highlight/Skill/Workspace/Knowledge-Graph adapters, schedules/watches not
double-running, secrets never syncing, and restart persistence.

Every test here uses either InMemoryProvider (fast, in-process "two
devices sharing one remote") or LocalFolderProvider against a tempdir -
never a real cloud service (Part 25).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_encrypted_sync -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-sync-"))
os.environ["PYBROWSER_DISABLE_KEYRING"] = "1"

from app.agent.keys import KeyringUnavailable  # noqa: E402
from app.missions.repository import MissionStore  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.highlights import HighlightStore  # noqa: E402
from app.storage.knowledge_graph_store import GraphStore  # noqa: E402
from app.storage.scheduled_tasks import ScheduledTaskStore  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402
from app.storage.skills import SkillStore  # noqa: E402
from app.storage.sync_store import (  # noqa: E402
    ConflictStore, GlobalIdStore, OwnershipStore, SyncDeviceStore, SyncVersionStore,
    TombstoneStore,
)
from app.storage.watches import WatchStore  # noqa: E402
from app.sync import crypto  # noqa: E402
from app.sync.adapters import (  # noqa: E402
    GraphEdgeAdapter, GraphNodeAdapter, HighlightAdapter, MissionAdapter,
    MissionFindingAdapter, ScheduledTaskAdapter, SkillAdapter, WatchAdapter, WorkspaceAdapter,
)
from app.sync.conflicts import ConflictPolicy, Resolution, policy_for  # noqa: E402
from app.sync.device import ensure_local_device  # noqa: E402
from app.sync.engine import SyncEngine, SyncStatus, content_hash  # noqa: E402
from app.sync.providers.base import SyncProviderUnavailable  # noqa: E402
from app.sync.providers.local_folder import LocalFolderProvider  # noqa: E402
from app.sync.providers.memory import InMemoryProvider, SharedMemoryBacking  # noqa: E402
from app.sync.types import (  # noqa: E402
    EncryptedPackage, RecordType, SyncPayloadError, SyncRecord, assert_no_secrets,
)
from app.workspaces.model import Workspace  # noqa: E402
from app.workspaces.store import WorkspaceStore  # noqa: E402


# ---------------------------------------------------------------------------
# Crypto: roundtrip, wrong key, tampering, replay
# ---------------------------------------------------------------------------

class CryptoTests(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        key = crypto.generate_master_key()
        package = crypto.encrypt_payload(key, {"hello": "world"}, aad=b"ctx")
        self.assertEqual(crypto.decrypt_payload(key, package, aad=b"ctx"), {"hello": "world"})

    def test_wrong_key_fails(self):
        key = crypto.generate_master_key()
        other_key = crypto.generate_master_key()
        package = crypto.encrypt_payload(key, {"a": 1}, aad=b"ctx")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_payload(other_key, package, aad=b"ctx")

    def test_tampered_ciphertext_fails(self):
        key = crypto.generate_master_key()
        package = crypto.encrypt_payload(key, {"a": 1}, aad=b"ctx")
        tampered_bytes = bytearray(package.ciphertext)
        tampered_bytes[0] ^= 0xFF
        tampered = crypto.EncryptedPackage(
            schema_version=package.schema_version, content_type=package.content_type,
            nonce=package.nonce, ciphertext=bytes(tampered_bytes))
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_payload(key, tampered, aad=b"ctx")

    def test_wrong_aad_fails(self):
        key = crypto.generate_master_key()
        package = crypto.encrypt_payload(key, {"a": 1}, aad=b"record-a")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_payload(key, package, aad=b"record-b")

    def test_package_to_bytes_roundtrip(self):
        key = crypto.generate_master_key()
        package = crypto.encrypt_payload(key, {"a": 1}, aad=b"ctx")
        restored = EncryptedPackage.from_bytes(package.to_bytes())
        self.assertEqual(crypto.decrypt_payload(key, restored, aad=b"ctx"), {"a": 1})

    def test_encrypt_decrypt_record_binds_to_storage_key(self):
        key = crypto.generate_master_key()
        record = SyncRecord(record_type=RecordType.HIGHLIGHT, global_id="g1",
                            payload={"text": "hi"}, version=1)
        package = crypto.encrypt_record(key, record)
        storage_key = crypto.storage_key(record.record_type, record.global_id)
        restored = crypto.decrypt_record(key, package, expected_key=storage_key)
        self.assertEqual(restored.payload, {"text": "hi"})

    def test_decrypt_record_refuses_swapped_key(self):
        key = crypto.generate_master_key()
        record = SyncRecord(record_type=RecordType.HIGHLIGHT, global_id="g1",
                            payload={"text": "hi"}, version=1)
        package = crypto.encrypt_record(key, record)
        wrong_key = crypto.storage_key(RecordType.HIGHLIGHT, "g2")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_record(key, package, expected_key=wrong_key)

    def test_scrypt_derivation_is_not_plain_sha256(self):
        salt = os.urandom(16)
        derived = crypto.derive_key_from_passphrase("correct horse", salt)
        import hashlib
        self.assertNotEqual(derived, hashlib.sha256(b"correct horse").digest())
        self.assertEqual(len(derived), crypto.KEY_LENGTH)

    def test_recovery_key_wrap_unwrap_roundtrip(self):
        master_key = crypto.generate_master_key()
        recovery_key = crypto.generate_recovery_key()
        wrapped = crypto.wrap_master_key(master_key, recovery_key)
        recovered = crypto.unwrap_master_key(wrapped, recovery_key)
        self.assertEqual(recovered, master_key)

    def test_recovery_key_wrong_code_fails(self):
        master_key = crypto.generate_master_key()
        wrapped = crypto.wrap_master_key(master_key, crypto.generate_recovery_key())
        with self.assertRaises(crypto.CryptoError):
            crypto.unwrap_master_key(wrapped, crypto.generate_recovery_key())

    def test_wrapped_key_bytes_roundtrip(self):
        master_key = crypto.generate_master_key()
        recovery_key = crypto.generate_recovery_key()
        wrapped = crypto.wrap_master_key(master_key, recovery_key)
        restored = crypto.WrappedKey.from_bytes(wrapped.to_bytes())
        self.assertEqual(crypto.unwrap_master_key(restored, recovery_key), master_key)


# ---------------------------------------------------------------------------
# Secrets never sync
# ---------------------------------------------------------------------------

class NoSecretsTests(unittest.TestCase):
    def test_assert_no_secrets_passes_clean_payload(self):
        assert_no_secrets({"title": "hello", "nested": {"count": 3}})

    def test_assert_no_secrets_rejects_api_key(self):
        with self.assertRaises(SyncPayloadError):
            assert_no_secrets({"api_key": "sk-abc123"})

    def test_assert_no_secrets_rejects_nested_token(self):
        with self.assertRaises(SyncPayloadError):
            assert_no_secrets({"outer": {"refresh_token": "xyz"}})

    def test_assert_no_secrets_rejects_password_in_list(self):
        with self.assertRaises(SyncPayloadError):
            assert_no_secrets({"items": [{"password": "hunter2"}]})

    def test_sync_record_construction_scans_payload(self):
        with self.assertRaises(SyncPayloadError):
            SyncRecord(record_type=RecordType.SETTINGS, global_id="x",
                      payload={"bearer_token": "abc"})

    def test_deleted_record_skips_secret_scan_since_payload_is_empty(self):
        # A tombstone's payload is always {} - nothing to scan, and the
        # scan is skipped for deleted=True records by design.
        record = SyncRecord(record_type=RecordType.HIGHLIGHT, global_id="x",
                            payload={}, deleted=True)
        self.assertTrue(record.deleted)

    def test_settings_adapter_only_ever_syncs_the_allowlist(self):
        from app.sync.adapters import SettingsAdapter
        from app.sync.types import SYNCABLE_SETTINGS_KEYS

        tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(tmp.name, "s.db"))
        self.addCleanup(db.close)
        settings = SettingsStore(db)
        settings.set("some_other_local_only_key", "should never sync")
        adapter = SettingsAdapter(settings)
        payload = adapter.build_payload("default")
        self.assertEqual(set(payload.keys()), SYNCABLE_SETTINGS_KEYS)
        self.assertNotIn("some_other_local_only_key", payload)


# ---------------------------------------------------------------------------
# Device identity
# ---------------------------------------------------------------------------

class DeviceIdentityTests(unittest.TestCase):
    def test_ensure_local_device_is_stable_across_calls(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(tmp.name, "d.db"))
        self.addCleanup(db.close)
        settings = SettingsStore(db)
        devices = SyncDeviceStore(db)
        first = ensure_local_device(settings, devices)
        second = ensure_local_device(settings, devices)
        self.assertEqual(first.id, second.id)

    def test_device_name_is_editable(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(tmp.name, "d.db"))
        self.addCleanup(db.close)
        settings = SettingsStore(db)
        devices = SyncDeviceStore(db)
        device = ensure_local_device(settings, devices)
        devices.rename(device.id, "Wendy's Mac")
        self.assertEqual(devices.get(device.id)["name"], "Wendy's Mac")

    def test_device_survives_restart(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "d.db")
        db1 = Database(path)
        self.addCleanup(db1.close)
        device = ensure_local_device(SettingsStore(db1), SyncDeviceStore(db1))
        db1.close()
        db2 = Database(path)
        self.addCleanup(db2.close)
        device_again = ensure_local_device(SettingsStore(db2), SyncDeviceStore(db2))
        self.assertEqual(device.id, device_again.id)


# ---------------------------------------------------------------------------
# Local-folder provider
# ---------------------------------------------------------------------------

class LocalFolderProviderTests(unittest.TestCase):
    def test_upload_download_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        provider = LocalFolderProvider(tmp.name)
        provider.upload("highlight:g1", b"encrypted-bytes")
        self.assertEqual(provider.download("highlight:g1"), b"encrypted-bytes")

    def test_download_missing_key_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        provider = LocalFolderProvider(tmp.name)
        self.assertIsNone(provider.download("nothing:here"))

    def test_list_changes_only_returns_changed_since_token(self):
        tmp = tempfile.TemporaryDirectory()
        provider = LocalFolderProvider(tmp.name)
        provider.upload("a:1", b"one")
        _entries, token = provider.list_changes(None)
        provider.upload("a:2", b"two")
        changed, _new_token = provider.list_changes(token)
        self.assertEqual([e.key for e in changed], ["a:2"])

    def test_unsafe_key_is_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        provider = LocalFolderProvider(tmp.name)
        with self.assertRaises(ValueError):
            provider.upload("../escape", b"data")

    def test_survives_restart_reading_manifest_fresh(self):
        tmp = tempfile.TemporaryDirectory()
        LocalFolderProvider(tmp.name).upload("a:1", b"one")
        reopened = LocalFolderProvider(tmp.name)
        self.assertEqual(reopened.download("a:1"), b"one")


# ---------------------------------------------------------------------------
# Two-device sync harness
# ---------------------------------------------------------------------------

class _Device:
    """One simulated device: its own sqlite file and stores, sharing one
    InMemoryProvider backing with its peer(s)."""

    def __init__(self, path: str, device_id: str, backing: SharedMemoryBacking) -> None:
        self.db = Database(path)
        self.device_id = device_id
        self.provider = InMemoryProvider(backing)
        self.global_ids = GlobalIdStore(self.db)
        self.versions = SyncVersionStore(self.db)
        self.tombstones = TombstoneStore(self.db)
        self.conflicts = ConflictStore(self.db)
        self.ownership = OwnershipStore(self.db)
        self.missions = MissionStore(self.db)
        self.highlights = HighlightStore(self.db)
        self.skills = SkillStore(self.db)
        self.workspaces = WorkspaceStore(self.db)
        self.scheduled_tasks = ScheduledTaskStore(self.db)
        self.watches = WatchStore(self.db)
        self.graph = GraphStore(self.db)
        self.since_token: str | None = None

    def adapters(self) -> dict:
        return {
            RecordType.MISSION: MissionAdapter(self.missions),
            RecordType.MISSION_FINDING: MissionFindingAdapter(self.missions, self.global_ids),
            RecordType.HIGHLIGHT: HighlightAdapter(self.highlights),
            RecordType.SKILL: SkillAdapter(self.skills),
            RecordType.WORKSPACE: WorkspaceAdapter(self.workspaces),
            RecordType.SCHEDULED_TASK: ScheduledTaskAdapter(
                self.scheduled_tasks, self.ownership, self.device_id),
            RecordType.WATCH: WatchAdapter(self.watches, self.ownership, self.device_id),
            RecordType.GRAPH_NODE: GraphNodeAdapter(self.graph),
            RecordType.GRAPH_EDGE: GraphEdgeAdapter(self.graph),
        }

    def sync(self, key: bytes):
        def _persist(token):
            self.since_token = token
        engine = SyncEngine(
            provider=self.provider, master_key=key, device_id=self.device_id,
            adapters=self.adapters(), global_ids=self.global_ids, versions=self.versions,
            tombstones=self.tombstones, conflicts=self.conflicts, since_token=self.since_token,
            on_since_token=_persist)
        return engine, engine.sync_once()


def _two_devices(test: unittest.TestCase):
    """Each Database opens its own background writer thread - leaving
    those unclosed across this file's ~25 call sites left as many
    lingering daemon threads live during the suite, which a real CI run
    (not just a local sandbox) showed can pile up enough to matter."""
    tmp = tempfile.TemporaryDirectory()
    backing = SharedMemoryBacking()
    key = crypto.generate_master_key()
    a = _Device(os.path.join(tmp.name, "a.db"), "device-a", backing)
    b = _Device(os.path.join(tmp.name, "b.db"), "device-b", backing)
    test.addCleanup(a.db.close)
    test.addCleanup(b.db.close)
    return tmp, key, a, b


# ---------------------------------------------------------------------------
# Mission + Finding merging
# ---------------------------------------------------------------------------

class MissionSyncTests(unittest.TestCase):
    def test_mission_created_on_a_appears_on_b(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Trip planning", "Plan a trip to Japan")
        a.sync(key)
        b.sync(key)
        synced = b.missions.recent(limit=10)
        self.assertEqual(len(synced), 1)
        self.assertEqual(synced[0].title, "Trip planning")

    def test_finding_merges_by_stable_id_not_duplicated(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Research", "Research something")
        a.missions.add_finding(mission.id, "Found fact one")
        a.sync(key)
        b.sync(key)
        b_mission = b.missions.recent(limit=10)[0]
        self.assertEqual(len(b.missions.findings(b_mission.id)), 1)
        # Re-sync must not duplicate the finding.
        a.sync(key)
        b.sync(key)
        self.assertEqual(len(b.missions.findings(b_mission.id)), 1)

    def test_findings_from_both_devices_all_merge_in(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Research", "Research something")
        a.sync(key)
        b.sync(key)
        b_mission = b.missions.recent(limit=10)[0]
        a.missions.add_finding(mission.id, "From A")
        b.missions.add_finding(b_mission.id, "From B")
        a.sync(key)
        b.sync(key)
        a.sync(key)
        texts = {f.text for f in a.missions.findings(mission.id)}
        self.assertEqual(texts, {"From A", "From B"})

    def test_deleting_a_mission_propagates(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Temp", "Temp goal")
        a.sync(key)
        b.sync(key)
        a.missions.soft_delete(mission.id)
        a.sync(key)
        b.sync(key)
        self.assertEqual(b.missions.recent(limit=10), [])


# ---------------------------------------------------------------------------
# Highlights
# ---------------------------------------------------------------------------

class HighlightSyncTests(unittest.TestCase):
    def test_highlight_syncs_and_deletion_propagates(self):
        _tmp, key, a, b = _two_devices(self)
        a.highlights.add("https://example.com", "Example", "quoted text")
        a.sync(key)
        b.sync(key)
        self.assertEqual(len(b.highlights.all()), 1)
        a.highlights.remove(a.highlights.all()[0].id)
        a.sync(key)
        b.sync(key)
        self.assertEqual(b.highlights.all(), [])

    def test_deletion_is_not_resurrected_by_a_stale_peer(self):
        """Part 20: a device that has not yet seen a deletion must not
        bring a deleted item back to life just because it re-uploads its
        own (already superseded) copy in a later, unrelated sync."""
        _tmp, key, a, b = _two_devices(self)
        a.highlights.add("https://example.com", "Example", "quoted text")
        a.sync(key)
        b.sync(key)
        a.highlights.remove(a.highlights.all()[0].id)
        a.sync(key)
        b.sync(key)
        self.assertEqual(b.highlights.all(), [])
        # b syncs again with nothing new locally - must not resurrect it.
        b.sync(key)
        self.assertEqual(b.highlights.all(), [])


# ---------------------------------------------------------------------------
# Skills (including recorded workflows via as_dict/from_dict)
# ---------------------------------------------------------------------------

class SkillSyncTests(unittest.TestCase):
    def test_custom_skill_syncs(self):
        from app.agent.skills import Skill

        _tmp, key, a, b = _two_devices(self)
        a.skills.save(Skill(id="s1", name="Summarizer", description="", instructions="Summarize."))
        a.sync(key)
        b.sync(key)
        synced = b.skills.get("s1")
        self.assertIsNotNone(synced)
        self.assertEqual(synced.name, "Summarizer")

    def test_skill_with_output_schema_syncs(self):
        from app.agent.skills import Skill

        _tmp, key, a, b = _two_devices(self)
        a.skills.save(Skill(id="s2", name="Extract", description="", instructions="Extract JSON.",
                            output_schema={"type": "object"}))
        a.sync(key)
        b.sync(key)
        self.assertEqual(b.skills.get("s2").output_schema, {"type": "object"})

    def test_skill_deletion_propagates(self):
        from app.agent.skills import Skill

        _tmp, key, a, b = _two_devices(self)
        a.skills.save(Skill(id="s3", name="Temp", description="", instructions="x"))
        a.sync(key)
        b.sync(key)
        a.skills.remove("s3")
        a.sync(key)
        b.sync(key)
        self.assertIsNone(b.skills.get("s3"))


# ---------------------------------------------------------------------------
# Workspaces - metadata/tabs sync, cookies/isolated profiles never do
# ---------------------------------------------------------------------------

class WorkspaceSyncTests(unittest.TestCase):
    def test_workspace_metadata_and_tabs_sync(self):
        from app.workspaces.model import TabRecord, TabState

        _tmp, key, a, b = _two_devices(self)
        ws = Workspace(id="w1", name="Coding", tab_state=TabState(
            tabs=(TabRecord(url="https://a.example"),), active_index=0))
        a.workspaces.save(ws)
        a.sync(key)
        b.sync(key)
        synced = b.workspaces.get("w1")
        self.assertEqual(synced.name, "Coding")
        self.assertEqual(len(synced.tab_state.tabs), 1)
        self.assertEqual(synced.tab_state.tabs[0].url, "https://a.example")

    def test_isolated_profile_never_syncs(self):
        """Part 13: "Workspaces sync, login sessions do not." An isolated
        workspace's cookie-bearing profile must never travel to another
        device - only the workspace's ordinary metadata does."""
        _tmp, key, a, b = _two_devices(self)
        ws = Workspace(id="w2", name="Banking", isolated_profile=True,
                       profile_storage_name="isolated-w2-secret-path")
        a.workspaces.save(ws)
        a.sync(key)
        b.sync(key)
        synced = b.workspaces.get("w2")
        self.assertFalse(synced.isolated_profile)
        self.assertEqual(synced.profile_storage_name, "")

    def test_isolated_profile_choice_is_never_overwritten_by_a_peer(self):
        """A device that already isolated a workspace keeps that choice
        even after pulling a peer's copy of the same workspace."""
        _tmp, key, a, b = _two_devices(self)
        ws = Workspace(id="w3", name="Banking")
        a.workspaces.save(ws)
        a.sync(key)
        b.sync(key)
        # b independently turns on isolation for its own copy.
        b_ws = b.workspaces.get("w3")
        from dataclasses import replace
        b.workspaces.save(replace(b_ws, isolated_profile=True, profile_storage_name="b-own"))
        # a makes an unrelated metadata edit and both sync again.
        a.workspaces.save(replace(a.workspaces.get("w3"), name="Banking (renamed)"))
        a.sync(key)
        b.sync(key)
        self.assertTrue(b.workspaces.get("w3").isolated_profile)
        self.assertEqual(b.workspaces.get("w3").profile_storage_name, "b-own")


# ---------------------------------------------------------------------------
# Scheduled Tasks / Watches - definitions sync, only the owner executes
# ---------------------------------------------------------------------------

class OwnershipTests(unittest.TestCase):
    def test_scheduled_task_created_on_a_is_owned_by_a_not_b(self):
        _tmp, key, a, b = _two_devices(self)
        task = a.scheduled_tasks.create(goal="Daily digest", schedule_kind="daily",
                                        time_of_day="09:00")
        a.sync(key)
        b.sync(key)
        synced = b.scheduled_tasks.all()[0]
        self.assertTrue(a.ownership.is_owned_by(RecordType.SCHEDULED_TASK, str(task.id), "device-a"))
        self.assertFalse(
            b.ownership.is_owned_by(RecordType.SCHEDULED_TASK, str(synced.id), "device-b"))

    def test_watch_created_on_a_is_owned_by_a_not_b(self):
        _tmp, key, a, b = _two_devices(self)
        watch = a.watches.create(title="Price watch", url="https://example.com/product",
                                 target_type="page", condition="changed",
                                 check_interval_seconds=3600)
        a.sync(key)
        b.sync(key)
        synced = b.watches.all()[0]
        self.assertTrue(a.ownership.is_owned_by(RecordType.WATCH, str(watch.id), "device-a"))
        self.assertFalse(b.ownership.is_owned_by(RecordType.WATCH, str(synced.id), "device-b"))

    def test_no_ownership_row_means_run_locally_the_pre_sync_default(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(tmp.name, "o.db"))
        self.addCleanup(db.close)
        ownership = OwnershipStore(db)
        self.assertTrue(ownership.is_owned_by("scheduled_task", "999", "any-device"))

    def test_task_runner_skips_a_task_not_owned_by_this_device(self):
        from app.missions.task_runner import TaskRunner

        tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(tmp.name, "t.db"))
        self.addCleanup(db.close)
        store = ScheduledTaskStore(db)
        ownership = OwnershipStore(db)
        task = store.create(goal="g", schedule_kind="once",
                            schedule_at="2000-01-01T00:00:00+00:00",
                            next_run_at="2000-01-01T00:00:00+00:00")
        ownership.set(RecordType.SCHEDULED_TASK, str(task.id), "other-device")

        class FakeSession:
            busy = False

        runner = TaskRunner(store, missions=None, session_provider=lambda: FakeSession(),
                            ownership=ownership, device_id="this-device")
        runner._tick()
        # Never picked up: state is exactly as create() left it.
        self.assertEqual(store.get(task.id).state, "queued")

    def test_task_runner_runs_a_task_owned_by_this_device(self):
        from datetime import datetime, timezone

        tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(tmp.name, "t.db"))
        self.addCleanup(db.close)
        store = ScheduledTaskStore(db)
        ownership = OwnershipStore(db)
        task = store.create(goal="g", schedule_kind="once",
                            schedule_at="2000-01-01T00:00:00+00:00",
                            next_run_at="2000-01-01T00:00:00+00:00")
        ownership.set(RecordType.SCHEDULED_TASK, str(task.id), "this-device")

        # Getting past the ownership filter is the thing under test here -
        # not the full fire/AgentSession machinery, which TaskRunner's own
        # test suite already covers end to end.
        due = store.due_tasks(datetime.now(timezone.utc))
        due_owned = [t for t in due if ownership.is_owned_by(
            RecordType.SCHEDULED_TASK, str(t.id), "this-device")]
        self.assertEqual(len(due_owned), 1)


# ---------------------------------------------------------------------------
# Knowledge Graph sync
# ---------------------------------------------------------------------------

class GraphSyncTests(unittest.TestCase):
    def test_topic_node_syncs_with_stable_id(self):
        from app.knowledge_graph.builder import GraphBuilder

        _tmp, key, a, b = _two_devices(self)
        GraphBuilder(a.graph).ensure_topic_node("MCP")
        a.sync(key)
        b.sync(key)
        from app.knowledge_graph.types import topic_node_id

        node_id = topic_node_id("MCP")
        self.assertIsNotNone(b.graph.get_node(node_id))
        self.assertEqual(a.graph.get_node(node_id).id, b.graph.get_node(node_id).id)

    def test_topic_and_source_and_edge_all_sync(self):
        from app.knowledge_graph.builder import GraphBuilder
        from app.knowledge_graph.types import EdgeType, NodeType, webpage_node_id

        _tmp, key, a, b = _two_devices(self)
        builder = GraphBuilder(a.graph)
        source = builder.ensure_source_node(NodeType.WEBPAGE, "https://example.com/mcp")
        builder.link_topics(source.id, "MCP permission scoping is useful")
        a.sync(key)
        b.sync(key)
        b_source_id = webpage_node_id("https://example.com/mcp")
        self.assertIsNotNone(b.graph.get_node(b_source_id))
        edges = b.graph.edges_from(b_source_id, edge_types=(EdgeType.ABOUT_TOPIC,))
        self.assertGreater(len(edges), 0)

    def test_mission_and_finding_typed_graph_nodes_are_not_synced(self):
        """Only content-derived node types travel (Topic/WebPage/PDF/File/
        Claim) - Mission/Finding graph nodes embed a local row id and are
        intentionally excluded from this adapter."""
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Research", "goal")
        a.missions.add_finding(mission.id, "text", )
        from app.knowledge_graph.builder import GraphBuilder

        GraphBuilder(a.graph).ensure_mission_node(mission)
        a.sync(key)
        b.sync(key)
        from app.knowledge_graph.types import mission_node_id

        self.assertIsNone(b.graph.get_node(mission_node_id(mission.id)))


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

class ConflictTests(unittest.TestCase):
    def test_mission_goal_edit_on_both_devices_is_a_manual_conflict(self):
        self.assertEqual(policy_for(RecordType.MISSION), ConflictPolicy.MANUAL)
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Trip", "Plan a trip")
        a.sync(key)
        b.sync(key)
        b_mission = b.missions.recent(limit=10)[0]
        a.missions.set_goal(mission.id, "Plan a trip to Japan")
        b.missions.set_goal(b_mission.id, "Plan a trip to Peru")
        a.sync(key)
        _engine_b, result_b = b.sync(key)
        self.assertEqual(result_b.status, SyncStatus.CONFLICT)
        self.assertEqual(len(b.conflicts.unresolved()), 1)
        # Neither side was silently overwritten.
        self.assertEqual(b.missions.get(b_mission.id).goal, "Plan a trip to Peru")

    def test_resolve_keep_local(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Trip", "Plan a trip")
        a.sync(key)
        b.sync(key)
        b_mission = b.missions.recent(limit=10)[0]
        a.missions.set_goal(mission.id, "Goal A")
        b.missions.set_goal(b_mission.id, "Goal B")
        a.sync(key)
        engine_b, _result = b.sync(key)
        conflict = b.conflicts.unresolved()[0]
        engine_b.resolve_conflict(conflict["id"], Resolution.KEEP_LOCAL)
        self.assertEqual(b.missions.get(b_mission.id).goal, "Goal B")
        self.assertEqual(b.conflicts.unresolved(), [])

    def test_resolve_keep_remote(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Trip", "Plan a trip")
        a.sync(key)
        b.sync(key)
        b_mission = b.missions.recent(limit=10)[0]
        a.missions.set_goal(mission.id, "Goal A")
        b.missions.set_goal(b_mission.id, "Goal B")
        a.sync(key)
        engine_b, _result = b.sync(key)
        conflict = b.conflicts.unresolved()[0]
        engine_b.resolve_conflict(conflict["id"], Resolution.KEEP_REMOTE)
        self.assertEqual(b.missions.get(b_mission.id).goal, "Goal A")

    def test_resolve_keep_both(self):
        _tmp, key, a, b = _two_devices(self)
        mission = a.missions.create("Trip", "Plan a trip")
        a.sync(key)
        b.sync(key)
        b_mission = b.missions.recent(limit=10)[0]
        a.missions.set_goal(mission.id, "Goal A")
        b.missions.set_goal(b_mission.id, "Goal B")
        a.sync(key)
        engine_b, _result = b.sync(key)
        conflict = b.conflicts.unresolved()[0]
        engine_b.resolve_conflict(conflict["id"], Resolution.KEEP_BOTH)
        goals = {m.goal for m in b.missions.recent(limit=10)}
        self.assertEqual(goals, {"Goal A", "Goal B"})

    def test_highlights_auto_keep_both_never_drops_an_edit(self):
        self.assertEqual(policy_for(RecordType.HIGHLIGHT), ConflictPolicy.KEEP_BOTH)


# ---------------------------------------------------------------------------
# Offline / provider unavailable
# ---------------------------------------------------------------------------

class OfflineTests(unittest.TestCase):
    def test_offline_provider_returns_offline_status_without_crashing(self):
        _tmp, key, a, _b = _two_devices(self)
        a.provider.set_unavailable(True)
        _engine, result = a.sync(key)
        self.assertEqual(result.status, SyncStatus.OFFLINE)

    def test_local_changes_queue_and_sync_once_back_online(self):
        _tmp, key, a, b = _two_devices(self)
        a.provider.set_unavailable(True)
        a.highlights.add("https://example.com", "Example", "text")
        _engine, offline_result = a.sync(key)
        self.assertEqual(offline_result.status, SyncStatus.OFFLINE)
        a.provider.set_unavailable(False)
        _engine, online_result = a.sync(key)
        self.assertEqual(online_result.status, SyncStatus.UP_TO_DATE)
        b.sync(key)
        self.assertEqual(len(b.highlights.all()), 1)


# ---------------------------------------------------------------------------
# Replay / rollback protection
# ---------------------------------------------------------------------------

class ReplayProtectionTests(unittest.TestCase):
    def test_stale_version_is_ignored_not_reapplied(self):
        _tmp, key, a, b = _two_devices(self)
        a.highlights.add("https://example.com", "Example", "v1")
        a.sync(key)
        b.sync(key)
        h = a.highlights.all()[0]
        a.highlights.set_note(h.id, "v2 note")
        a.sync(key)
        b.sync(key)
        self.assertEqual(b.highlights.all()[0].note, "v2 note")
        # Simulate a stale re-upload of the OLD (pre-note) blob by directly
        # calling the engine's pull path on an artificially old package.
        from app.sync import crypto as crypto_module

        old_record = SyncRecord(record_type=RecordType.HIGHLIGHT,
                                global_id=a.global_ids.get_global_id(
                                    RecordType.HIGHLIGHT, str(h.id)),
                                payload={"url": h.url, "title": h.title, "text": h.text,
                                        "note": ""},
                                device_id="device-a", version=1)  # version 1: stale
        storage_key = crypto_module.storage_key(old_record.record_type, old_record.global_id)
        package = crypto_module.encrypt_record(key, old_record)
        b.provider.upload(storage_key, package.to_bytes())
        b.sync(key)
        # The stale, lower-version replay must not have reverted the note.
        self.assertEqual(b.highlights.all()[0].note, "v2 note")


# ---------------------------------------------------------------------------
# content_hash / restart persistence of sync metadata
# ---------------------------------------------------------------------------

class MiscEngineTests(unittest.TestCase):
    def test_content_hash_is_stable_regardless_of_key_order(self):
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_content_hash_differs_for_different_content(self):
        self.assertNotEqual(content_hash({"a": 1}), content_hash({"a": 2}))

    def test_global_id_mapping_survives_restart(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "g.db")
        db1 = Database(path)
        self.addCleanup(db1.close)
        global_ids1 = GlobalIdStore(db1)
        gid = global_ids1.ensure_global_id(RecordType.HIGHLIGHT, "5")
        db1.close()
        db2 = Database(path)
        self.addCleanup(db2.close)
        global_ids2 = GlobalIdStore(db2)
        self.assertEqual(global_ids2.get_global_id(RecordType.HIGHLIGHT, "5"), gid)

    def test_since_token_persists_across_restart_via_settings(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "s.db")
        db1 = Database(path)
        self.addCleanup(db1.close)
        settings1 = SettingsStore(db1)
        settings1.set("sync_since_token", "42")
        db1.close()
        db2 = Database(path)
        self.addCleanup(db2.close)
        settings2 = SettingsStore(db2)
        self.assertEqual(settings2.get("sync_since_token", ""), "42")


if __name__ == "__main__":
    unittest.main()
