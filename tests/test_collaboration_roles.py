"""Collaboration roles, as far as the architecture can enforce them.

Roles are advisory between people who already hold the shared key: there is no
signature scheme, so a malicious key holder can claim any role. What IS enforced
is (a) this device refuses local writes it has no role for and (b) it does not
apply incoming changes from a device it knows to be a viewer or removed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collaboration.types import Role  # noqa: E402
from tests.test_collaboration import Device, _mk_mission  # noqa: E402


class RoleEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pybrowser-roles-")
        self.folder = tempfile.mkdtemp(prefix="pybrowser-roles-folder-")
        self.a = Device(self.tmp, "a")
        self.addCleanup(self.a.db.close)
        self.mid = _mk_mission(self.a)
        self.a.collab.share_mission(self.mid, self.folder)
        self.a.collab.sync_now(self.mid)

    def _join(self, name, role):
        invite = self.a.collab.create_invite(self.mid, role=role)
        dev = Device(self.tmp, name)
        self.addCleanup(dev.db.close)
        return dev, dev.collab.join_mission(invite.data, invite.passphrase)

    def test_viewer_is_refused_locally_editor_and_owner_are_not(self):
        v, vmid = self._join("v", Role.VIEWER)
        e, emid = self._join("e", Role.EDITOR)
        self.assertIn("viewer", v.collab.write_refusal(vmid))
        self.assertIsNone(e.collab.write_refusal(emid))
        self.assertIsNone(self.a.collab.write_refusal(self.mid))

    def test_unshared_mission_is_never_refused(self):
        other = _mk_mission(self.a, title="Private")
        self.assertIsNone(self.a.collab.write_refusal(other))

    def test_finding_forged_by_a_viewer_device_is_not_applied(self):
        v, vmid = self._join("v", Role.VIEWER)
        v.collab.sync_now(vmid)
        self.a.collab.sync_now(self.mid)  # A learns V's participant record
        # Bypass the local guard on purpose: a modified client.
        v.missions.add_finding(vmid, "Forged by a viewer")
        v.collab.sync_now(vmid)
        result = self.a.collab.sync_now(self.mid)
        self.assertGreaterEqual(result.rejected, 1)
        self.assertNotIn("Forged by a viewer", {f.text for f in self.a.missions.findings(self.mid)})

    def test_editor_findings_are_applied(self):
        e, emid = self._join("e", Role.EDITOR)
        e.collab.sync_now(emid)
        self.a.collab.sync_now(self.mid)
        e.missions.add_finding(emid, "Real finding")
        e.collab.sync_now(emid)
        result = self.a.collab.sync_now(self.mid)
        self.assertEqual(result.rejected, 0)
        self.assertIn("Real finding", {f.text for f in self.a.missions.findings(self.mid)})

    def test_removed_participant_changes_are_not_applied(self):
        e, emid = self._join("e", Role.EDITOR)
        e.collab.sync_now(emid)
        self.a.collab.sync_now(self.mid)
        self.a.collab.remove_participant(self.mid, e.collab.device.id, rotate_key=False)
        self.a.collab.sync_now(self.mid)
        e.missions.add_finding(emid, "After removal")
        e.collab.sync_now(emid)
        self.a.collab.sync_now(self.mid)
        self.assertNotIn("After removal", {f.text for f in self.a.missions.findings(self.mid)})

    def test_viewers_can_still_comment_only_when_allowed(self):
        v, vmid = self._join("v", Role.VIEWER)
        from app.collaboration.service import CollaborationError
        with self.assertRaises(CollaborationError):
            v.collab.add_comment(vmid, "mission", str(vmid), "hi")


if __name__ == "__main__":
    unittest.main()
