"""MissionGraphStore: persistence for the Mission Execution Graph, and
restart recovery - see app/storage/mission_graph.py.

Run with:
    python -m unittest tests.test_mission_graph_store -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import Database, MissionGraphStore  # noqa: E402
from app.storage.mission_graph import recover_after_restart  # noqa: E402


def _make_store():
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO missions (title, goal, created_at, updated_at) VALUES (?,?,?,?)",
              ("Mission", "goal", now, now))
    return MissionGraphStore(db), db, tmp, 1


class MissionGraphStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp, self.mission_id = _make_store()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_create_and_get_round_trip(self) -> None:
        node = self.store.create_node(
            self.mission_id, node_type="research", role="researcher",
            title="Look into A", instructions="find specs")
        self.assertIsNotNone(node)
        fetched = self.store.get(node.id)
        self.assertEqual(fetched.title, "Look into A")
        self.assertEqual(fetched.state, "pending")
        self.assertEqual(fetched.dependencies, ())
        self.assertEqual(fetched.attempt_count, 0)
        self.assertFalse(fetched.write_attempted)

    def test_dependencies_round_trip_as_ordered_ids(self) -> None:
        n1 = self.store.create_node(self.mission_id, node_type="research", role="researcher",
                                    title="A", instructions="x")
        n2 = self.store.create_node(self.mission_id, node_type="write", role="writer",
                                    title="B", instructions="y")
        self.store.set_dependencies(n2.id, [n1.id])
        self.assertEqual(self.store.get(n2.id).dependencies, (n1.id,))

    def test_nodes_for_mission_returns_in_creation_order(self) -> None:
        ids = [self.store.create_node(self.mission_id, node_type="research", role="researcher",
                                      title=f"T{i}", instructions="x").id
              for i in range(3)]
        nodes = self.store.nodes_for_mission(self.mission_id)
        self.assertEqual([n.id for n in nodes], ids)

    def test_count_for_mission(self) -> None:
        self.assertEqual(self.store.count_for_mission(self.mission_id), 0)
        self.store.create_node(self.mission_id, node_type="research", role="researcher",
                               title="A", instructions="x")
        self.assertEqual(self.store.count_for_mission(self.mission_id), 1)

    def test_record_start_sets_running_and_bumps_attempt_count(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="research", role="researcher",
                                      title="A", instructions="x")
        self.store.record_start(node.id)
        updated = self.store.get(node.id)
        self.assertEqual(updated.state, "running")
        self.assertEqual(updated.attempt_count, 1)
        self.assertIsNotNone(updated.started_at)

    def test_record_terminal_sets_completion_fields(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="write", role="writer",
                                      title="A", instructions="x")
        self.store.record_start(node.id)
        self.store.record_terminal(node.id, state="completed", result_summary="done well",
                                   findings_added=2)
        updated = self.store.get(node.id)
        self.assertEqual(updated.state, "completed")
        self.assertEqual(updated.result_summary, "done well")
        self.assertEqual(updated.findings_added, 2)
        self.assertIsNotNone(updated.completed_at)

    def test_write_attempted_flag_round_trips(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="browse", role="browser_operator",
                                      title="A", instructions="x")
        self.store.set_write_attempted(node.id, True)
        self.assertTrue(self.store.get(node.id).write_attempted)

    def test_reset_for_retry_clears_error_and_write_attempted(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="research", role="researcher",
                                      title="A", instructions="x")
        self.store.set_write_attempted(node.id, True)
        self.store.record_terminal(node.id, state="failed", error="broke")
        self.store.reset_for_retry(node.id)
        updated = self.store.get(node.id)
        self.assertEqual(updated.state, "pending")
        self.assertEqual(updated.error, "")
        self.assertFalse(updated.write_attempted)


class RecoverAfterRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp, self.mission_id = _make_store()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_stranded_read_only_node_becomes_pending(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="research", role="researcher",
                                      title="A", instructions="x")
        self.store.record_start(node.id)
        recovered = recover_after_restart(self.store)
        self.assertEqual([n.id for n in recovered], [node.id])
        self.assertEqual(self.store.get(node.id).state, "pending")

    def test_a_stranded_write_node_becomes_needs_review(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="browse", role="browser_operator",
                                      title="A", instructions="x")
        self.store.record_start(node.id)
        self.store.set_write_attempted(node.id, True)
        recover_after_restart(self.store)
        self.assertEqual(self.store.get(node.id).state, "needs_review")

    def test_a_node_waiting_for_approval_with_no_write_yet_becomes_pending(self) -> None:
        node = self.store.create_node(self.mission_id, node_type="browse", role="browser_operator",
                                      title="A", instructions="x")
        self.store.record_start(node.id)
        self.store.set_state(node.id, "waiting_for_approval")
        recover_after_restart(self.store)
        self.assertEqual(self.store.get(node.id).state, "pending")

    def test_completed_and_pending_nodes_are_left_alone(self) -> None:
        completed = self.store.create_node(self.mission_id, node_type="research",
                                           role="researcher", title="A", instructions="x")
        self.store.record_start(completed.id)
        self.store.record_terminal(completed.id, state="completed")
        pending = self.store.create_node(self.mission_id, node_type="write", role="writer",
                                         title="B", instructions="y")
        recovered = recover_after_restart(self.store)
        self.assertEqual(recovered, [])
        self.assertEqual(self.store.get(completed.id).state, "completed")
        self.assertEqual(self.store.get(pending.id).state, "pending")

    def test_recovery_never_guesses_it_always_asks(self) -> None:
        """The write-attempted node must never come back as pending or
        completed - only needs_review, which forces an explicit decision."""
        node = self.store.create_node(self.mission_id, node_type="mcp_action",
                                      role="browser_operator", title="A", instructions="x")
        self.store.record_start(node.id)
        self.store.set_write_attempted(node.id, True)
        recover_after_restart(self.store)
        state = self.store.get(node.id).state
        self.assertNotIn(state, ("pending", "completed", "running"))
        self.assertEqual(state, "needs_review")


if __name__ == "__main__":
    unittest.main()
