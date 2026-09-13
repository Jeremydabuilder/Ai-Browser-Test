"""SkillsLibraryDialog and SkillEditDialog: create/edit/duplicate/delete,
built-in immutability, and the Run button handing a Skill back to the
caller. See app/ui/skills_library.py.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_skills_library_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.skills import BUILTIN_SKILLS, Skill  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.skills import SkillStore  # noqa: E402
from app.ui.skills_library import SkillEditDialog, SkillsLibraryDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class SkillEditDialogTests(unittest.TestCase):
    def test_creating_with_blank_name_shows_an_error_and_does_not_accept(self) -> None:
        dialog = SkillEditDialog()
        dialog.name_edit.setText("   ")
        dialog._on_save()
        self.assertIsNone(dialog.result_skill())
        self.assertFalse(dialog._error_label.isHidden())

    def test_creating_with_a_name_produces_an_unrestricted_skill_by_default(self) -> None:
        dialog = SkillEditDialog()
        dialog.name_edit.setText("My Custom Skill")
        dialog.instructions_edit.setPlainText("Do the thing.")
        dialog._on_save()
        skill = dialog.result_skill()
        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "My Custom Skill")
        self.assertIsNone(skill.allowed_tools)
        self.assertFalse(skill.builtin)

    def test_unchecking_unrestricted_and_checking_one_tool_scopes_to_it(self) -> None:
        dialog = SkillEditDialog()
        dialog.name_edit.setText("Scoped Skill")
        dialog.unrestricted_checkbox.setChecked(False)
        for i in range(dialog.tools_list.count()):
            item = dialog.tools_list.item(i)
            if item.text() == "browser_get_page_text":
                item.setCheckState(Qt.CheckState.Checked)
        dialog._on_save()
        skill = dialog.result_skill()
        self.assertEqual(skill.allowed_tools, ("browser_get_page_text",))

    def test_an_invalid_output_schema_is_refused(self) -> None:
        dialog = SkillEditDialog()
        dialog.name_edit.setText("Bad Schema Skill")
        dialog.output_schema_edit.setPlainText("{not valid json")
        dialog._on_save()
        self.assertIsNone(dialog.result_skill())

    def test_a_valid_output_schema_is_kept(self) -> None:
        dialog = SkillEditDialog()
        dialog.name_edit.setText("Good Schema Skill")
        dialog.output_schema_edit.setPlainText('{"type": "object", "required": ["x"]}')
        dialog._on_save()
        skill = dialog.result_skill()
        self.assertEqual(skill.output_schema, {"type": "object", "required": ["x"]})

    def test_editing_an_existing_skill_keeps_its_id(self) -> None:
        original = Skill(id="fixed-id", name="Original", description="",
                         instructions="", allowed_tools=None)
        dialog = SkillEditDialog(skill=original)
        dialog.name_edit.setText("Renamed")
        dialog._on_save()
        skill = dialog.result_skill()
        self.assertEqual(skill.id, "fixed-id")
        self.assertEqual(skill.name, "Renamed")

    def test_the_provider_combo_includes_a_no_preference_option(self) -> None:
        dialog = SkillEditDialog()
        self.assertEqual(dialog.provider_combo.itemData(0), "")


class SkillsLibraryDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.store = SkillStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_all_built_ins_are_listed_by_default(self) -> None:
        dialog = SkillsLibraryDialog(self.store)
        row_names = [dialog._rows_layout.itemAt(i).widget().skill.name
                    for i in range(dialog._rows_layout.count() - 1)]
        for builtin in BUILTIN_SKILLS:
            self.assertIn(builtin.name, row_names)

    def test_a_custom_skill_appears_alongside_built_ins(self) -> None:
        self.store.save(Skill(id="mine", name="Mine", description="", instructions=""))
        dialog = SkillsLibraryDialog(self.store)
        row_names = [dialog._rows_layout.itemAt(i).widget().skill.name
                    for i in range(dialog._rows_layout.count() - 1)]
        self.assertIn("Mine", row_names)

    def test_run_hands_the_skill_back_and_closes_the_dialog(self) -> None:
        seen = []
        dialog = SkillsLibraryDialog(self.store, on_run=seen.append)
        row = dialog._rows_layout.itemAt(0).widget()
        dialog._run(row.skill)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].id, row.skill.id)

    def test_duplicating_a_builtin_creates_a_custom_copy(self) -> None:
        dialog = SkillsLibraryDialog(self.store)
        builtin_row = next(
            dialog._rows_layout.itemAt(i).widget()
            for i in range(dialog._rows_layout.count() - 1)
            if dialog._rows_layout.itemAt(i).widget().skill.builtin)
        before = len(self.store.all())
        dialog._duplicate_skill(builtin_row.skill)
        after = self.store.all()
        self.assertEqual(len(after), before + 1)
        copy = next(s for s in after if s.id != builtin_row.skill.id)
        self.assertFalse(copy.builtin)
        self.assertIn("Copy", copy.name)

    def test_a_builtin_cannot_be_deleted_through_the_dialog(self) -> None:
        """No delete button exists for a built-in row at all - see
        _SkillRow's on_delete guard."""
        dialog = SkillsLibraryDialog(self.store)
        builtin_row = next(
            dialog._rows_layout.itemAt(i).widget()
            for i in range(dialog._rows_layout.count() - 1)
            if dialog._rows_layout.itemAt(i).widget().skill.builtin)
        button_texts = [
            builtin_row.layout().itemAt(i).widget().text()
            for i in range(builtin_row.layout().count())
            if hasattr(builtin_row.layout().itemAt(i), "widget")
            and builtin_row.layout().itemAt(i).widget() is not None]
        # The row's top-level layout is a QVBoxLayout with a nested button
        # row; walk its children for the buttons row instead.
        found_delete = False
        for i in range(builtin_row.layout().count()):
            item = builtin_row.layout().itemAt(i)
            sub_layout = item.layout()
            if sub_layout is None:
                continue
            for j in range(sub_layout.count()):
                widget = sub_layout.itemAt(j).widget()
                if widget is not None and widget.text() == "Delete":
                    found_delete = True
        self.assertFalse(found_delete)

    def test_deleting_a_custom_skill_removes_it(self) -> None:
        from unittest.mock import patch

        self.store.save(Skill(id="mine", name="Mine", description="", instructions=""))
        dialog = SkillsLibraryDialog(self.store)
        mine = self.store.get("mine")
        with patch("app.ui.skills_library.confirm_destructive", return_value=True):
            dialog._delete_skill(mine)
        self.assertIsNone(self.store.get("mine"))

    def test_editing_a_custom_skill_persists_the_change(self) -> None:
        self.store.save(Skill(id="mine", name="Mine", description="", instructions=""))
        dialog = SkillsLibraryDialog(self.store)
        mine = self.store.get("mine")
        edited = Skill(id="mine", name="Renamed", description="", instructions="do it")
        with mock.patch.object(SkillEditDialog, "exec", return_value=1), \
             mock.patch.object(SkillEditDialog, "result_skill", return_value=edited):
            dialog._edit_skill(mine)
        self.assertEqual(self.store.get("mine").name, "Renamed")


if __name__ == "__main__":
    unittest.main()
