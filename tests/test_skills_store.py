"""SkillStore: persistence for custom Skills only - built-ins never appear
here. See app/agent/skills.py and app/storage/skills.py.

Run with:
    python -m unittest tests.test_skills_store -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.skills import Skill  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.skills import SkillStore  # noqa: E402


class SkillStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.store = SkillStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def _skill(self, **overrides) -> Skill:
        defaults = dict(id="my-skill", name="My Skill", description="Does a thing",
                        instructions="Do the thing.", allowed_tools=("browser_get_page_text",))
        defaults.update(overrides)
        return Skill(**defaults)

    def test_saving_a_skill_persists_its_fields(self) -> None:
        saved = self.store.save(self._skill())
        self.assertIsNotNone(saved)
        self.assertEqual(saved.name, "My Skill")
        self.assertEqual(saved.allowed_tools, ("browser_get_page_text",))
        self.assertFalse(saved.builtin)

    def test_a_skill_with_no_allowlist_round_trips_as_none(self) -> None:
        self.store.save(self._skill(id="unrestricted", allowed_tools=None))
        fetched = self.store.get("unrestricted")
        self.assertIsNone(fetched.allowed_tools)

    def test_an_output_schema_round_trips(self) -> None:
        schema = {"type": "object", "required": ["summary"]}
        self.store.save(self._skill(id="with-schema", output_schema=schema))
        fetched = self.store.get("with-schema")
        self.assertEqual(fetched.output_schema, schema)

    def test_saving_a_blank_name_is_refused(self) -> None:
        result = self.store.save(self._skill(name="   "))
        self.assertIsNone(result)
        self.assertEqual(self.store.all(), [])

    def test_updating_an_existing_skill_keeps_its_id(self) -> None:
        self.store.save(self._skill())
        self.store.save(self._skill(name="Renamed"))
        rows = self.store.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Renamed")

    def test_all_lists_every_saved_skill(self) -> None:
        self.store.save(self._skill(id="a", name="A"))
        self.store.save(self._skill(id="b", name="B"))
        self.assertEqual({s.id for s in self.store.all()}, {"a", "b"})

    def test_removing_deletes_it(self) -> None:
        self.store.save(self._skill())
        self.store.remove("my-skill")
        self.assertIsNone(self.store.get("my-skill"))

    def test_contains_reflects_presence(self) -> None:
        self.assertFalse(self.store.contains("my-skill"))
        self.store.save(self._skill())
        self.assertTrue(self.store.contains("my-skill"))

    def test_persists_across_reopening_the_database(self) -> None:
        self.store.save(self._skill())
        self.db.close()
        reopened = Database(self.db.path)
        try:
            self.assertIsNotNone(SkillStore(reopened).get("my-skill"))
        finally:
            reopened.close()

    def test_default_context_kinds_round_trip(self) -> None:
        self.store.save(self._skill(default_context_kinds=("tab", "mission")))
        fetched = self.store.get("my-skill")
        self.assertEqual(fetched.default_context_kinds, ("tab", "mission"))


if __name__ == "__main__":
    unittest.main()
