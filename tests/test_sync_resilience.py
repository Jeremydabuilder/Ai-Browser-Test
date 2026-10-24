"""Sync failure modes through a REAL shared folder (LocalFolderProvider), with
two devices that each have their own database and device id. This is still one
process - it proves the protocol and the on-disk behaviour, not that a cloud
drive client behaves; that remains a field test."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sync import crypto  # noqa: E402
from app.sync.engine import SyncStatus  # noqa: E402
from app.sync.providers.local_folder import LocalFolderProvider  # noqa: E402
from app.sync.providers.memory import SharedMemoryBacking  # noqa: E402
from tests.test_encrypted_sync import _Device  # noqa: E402


def _devices(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    folder = Path(tmp.name) / "shared"
    key = crypto.generate_master_key()
    a = _Device(os.path.join(tmp.name, "a.db"), "device-a", SharedMemoryBacking())
    b = _Device(os.path.join(tmp.name, "b.db"), "device-b", SharedMemoryBacking())
    for d in (a, b):
        d.provider = LocalFolderProvider(folder)
        test.addCleanup(d.db.close)
    return folder, key, a, b


def _record_file(folder: Path, n: int = 0) -> Path:
    return sorted((folder / "records").glob("*.enc"))[n]


class FolderSyncTests(unittest.TestCase):
    def test_round_trip_through_a_real_folder(self):
        folder, key, a, b = _devices(self)
        a.missions.create("Trip", "Plan")
        self.assertEqual(a.sync(key)[1].status, SyncStatus.UP_TO_DATE)
        b.sync(key)
        self.assertEqual([m.title for m in b.missions.recent(limit=5)], ["Trip"])

    def test_plaintext_never_reaches_the_folder(self):
        folder, key, a, _b = _devices(self)
        a.missions.create("Secret-ish mission title", "Secret-ish goal")
        a.sync(key)
        blob = b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())
        self.assertNotIn(b"Secret-ish", blob)

    def test_truncated_record_does_not_crash_and_is_retried(self):
        folder, key, a, b = _devices(self)
        a.missions.create("Trip", "Plan")
        a.sync(key)
        good = _record_file(folder).read_bytes()
        _record_file(folder).write_bytes(good[:10])  # a half-synced file
        result = b.sync(key)[1]
        self.assertEqual(result.status, SyncStatus.ERROR)
        self.assertEqual(result.failed, 1)
        self.assertEqual(b.missions.recent(limit=5), [])
        # The cloud client finishes the upload: the next pass must pick it up
        # (the sync token was held back, not advanced past the bad file).
        _record_file(folder).write_bytes(good)
        result = b.sync(key)[1]
        self.assertEqual(result.status, SyncStatus.UP_TO_DATE)
        self.assertEqual([m.title for m in b.missions.recent(limit=5)], ["Trip"])

    def test_wrong_key_is_an_error_not_a_crash_or_a_silent_success(self):
        folder, key, a, b = _devices(self)
        a.missions.create("Trip", "Plan")
        a.sync(key)
        result = b.sync(crypto.generate_master_key())[1]
        self.assertEqual(result.status, SyncStatus.ERROR)
        self.assertIn("different key", result.error)
        self.assertEqual(b.missions.recent(limit=5), [])

    def test_record_swapped_into_another_slot_is_rejected(self):
        folder, key, a, b = _devices(self)
        a.missions.create("One", "g")
        a.missions.create("Two", "g")
        a.sync(key)
        f0, f1 = _record_file(folder, 0), _record_file(folder, 1)
        d0, d1 = f0.read_bytes(), f1.read_bytes()
        f0.write_bytes(d1)
        f1.write_bytes(d0)
        result = b.sync(key)[1]
        self.assertEqual(result.status, SyncStatus.ERROR)
        self.assertEqual(b.missions.recent(limit=5), [])

    def test_corrupt_manifest_is_rebuilt_from_the_record_files(self):
        folder, key, a, b = _devices(self)
        a.missions.create("Trip", "Plan")
        a.sync(key)
        (folder / "manifest.json").write_text("{not json", encoding="utf-8")
        result = b.sync(key)[1]
        self.assertEqual(result.status, SyncStatus.UP_TO_DATE)
        self.assertEqual(len(b.missions.recent(limit=5)), 1)

    def test_manifest_lost_to_a_concurrent_writer_is_healed(self):
        """Two devices share one manifest file with no lock: if one overwrites
        the other's index, the record file still exists and must be found."""
        folder, key, a, b = _devices(self)
        a.missions.create("From A", "g")
        a.sync(key)
        stale = (folder / "manifest.json").read_text()
        b.sync(key)
        b.missions.create("From B", "g")
        b.sync(key)
        (folder / "manifest.json").write_text(stale)  # A's write clobbers B's index
        a.sync(key)
        titles = sorted(m.title for m in a.missions.recent(limit=5))
        self.assertEqual(titles, ["From A", "From B"])

    def test_no_partial_files_or_temp_litter_after_uploads(self):
        folder, key, a, _b = _devices(self)
        for i in range(5):
            a.missions.create(f"M{i}", "g")
        a.sync(key)
        names = [p.name for p in folder.rglob("*") if p.is_file()]
        self.assertFalse([n for n in names if n.endswith(".tmp")], names)

    def test_unavailable_folder_reports_offline_and_recovers(self):
        folder, key, a, b = _devices(self)
        a.missions.create("Trip", "Plan")
        blocker = folder.parent / "blocked"
        blocker.write_text("a file where the folder should be")
        a.provider = LocalFolderProvider(blocker / "inside")
        result = a.sync(key)[1]
        self.assertEqual(result.status, SyncStatus.OFFLINE)
        a.provider = LocalFolderProvider(folder)  # folder is reachable again
        self.assertEqual(a.sync(key)[1].status, SyncStatus.UP_TO_DATE)
        b.sync(key)
        self.assertEqual(len(b.missions.recent(limit=5)), 1)

    def test_failed_tombstone_upload_is_retried_not_forgotten(self):
        folder, key, a, b = _devices(self)
        m = a.missions.create("Temp", "g")
        a.sync(key)
        b.sync(key)
        a.missions.soft_delete(m.id)
        good = a.provider
        class Down(LocalFolderProvider):
            def upload(self, k, d):
                from app.sync.providers.base import SyncProviderUnavailable
                raise SyncProviderUnavailable("down")
        a.provider = Down(folder)
        self.assertEqual(a.sync(key)[1].status, SyncStatus.OFFLINE)
        a.provider = good
        a.sync(key)
        b.sync(key)
        self.assertEqual(b.missions.recent(limit=5), [])

    def test_both_devices_edit_offline_then_converge_without_losing_either(self):
        folder, key, a, b = _devices(self)
        m = a.missions.create("Research", "g")
        a.sync(key)
        b.sync(key)
        bm = b.missions.recent(limit=5)[0]
        a.missions.add_finding(m.id, "A finding")
        b.missions.add_finding(bm.id, "B finding")
        for _ in range(2):
            a.sync(key)
            b.sync(key)
        for dev, mid in ((a, m.id), (b, bm.id)):
            self.assertEqual({f.text for f in dev.missions.findings(mid)},
                             {"A finding", "B finding"})

    def test_device_ids_are_independent(self):
        _folder, _key, a, b = _devices(self)
        self.assertNotEqual(a.device_id, b.device_id)
        self.assertIsNot(a.db, b.db)


if __name__ == "__main__":
    unittest.main()
