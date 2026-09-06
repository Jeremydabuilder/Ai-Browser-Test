"""Configure AI Agent: the Preset shortcut over model + effort.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_agent_presets -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import PRESETS, preset_for  # noqa: E402
from app.ui.agent_setup import ApiKeyDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class PresetLookupTests(unittest.TestCase):
    """The pure mapping in app/agent/config.py, independent of any widget."""

    def test_every_preset_names_a_real_model_and_effort(self) -> None:
        from app.agent.config import _BY_ID, _EFFORT_IDS

        for _id, _label, model_id, effort in PRESETS:
            self.assertIn(model_id, _BY_ID, f"{model_id} is not in the model catalogue")
            self.assertIn(effort, _EFFORT_IDS, f"{effort} is not a real effort level")

    def test_a_known_pair_resolves_to_its_preset(self) -> None:
        _id, _label, model_id, effort = PRESETS[0]
        self.assertEqual(preset_for(model_id, effort), _id)

    def test_an_unmatched_pair_resolves_to_nothing(self) -> None:
        self.assertEqual(preset_for("claude-opus-5", "low"), "")

    def test_presets_are_distinct_from_each_other(self) -> None:
        pairs = [(model_id, effort) for _id, _label, model_id, effort in PRESETS]
        self.assertEqual(len(pairs), len(set(pairs)), "two presets resolve to the same pair")


class PresetDialogTests(unittest.TestCase):
    """The dialog: picking a preset sets both dropdowns; changing either
    dropdown by hand falls back to (or back into) the matching preset."""

    def setUp(self) -> None:
        self.dialog = ApiKeyDialog(None, None)

    def tearDown(self) -> None:
        self.dialog.deleteLater()
        _app.processEvents()

    def _preset_index(self, preset_id: str) -> int:
        index = self.dialog.preset_box.findData(preset_id)
        self.assertGreaterEqual(index, 0, f"no such preset: {preset_id}")
        return index

    def test_the_default_selection_matches_the_default_config(self) -> None:
        # AgentConfig()'s own defaults are the "balance" preset's pair (see
        # PRESETS in app/agent/config.py) - the dialog should show it as
        # such, not as "Custom", the moment it opens with nothing configured.
        self.assertEqual(self.dialog.preset_box.currentData(), "balance")

    def test_picking_a_preset_sets_both_dropdowns(self) -> None:
        _id, _label, model_id, effort = next(p for p in PRESETS if p[0] == "fast")
        self.dialog.preset_box.setCurrentIndex(self._preset_index("fast"))
        self.assertEqual(self.dialog.model_box.currentData(), model_id)
        self.assertEqual(self.dialog.effort_box.currentData(), effort)

    def test_every_preset_is_reachable_and_round_trips(self) -> None:
        for preset_id, _label, model_id, effort in PRESETS:
            self.dialog.preset_box.setCurrentIndex(self._preset_index(preset_id))
            self.assertEqual(self.dialog.model_box.currentData(), model_id)
            self.assertEqual(self.dialog.effort_box.currentData(), effort)
            self.assertEqual(self.dialog.preset_box.currentData(), preset_id)

    def test_changing_the_model_by_hand_falls_back_to_custom(self) -> None:
        self.dialog.preset_box.setCurrentIndex(self._preset_index("fast"))
        # Fable 5 + low effort matches no preset in the table.
        other_model = self.dialog.model_box.findData("claude-fable-5")
        self.dialog.model_box.setCurrentIndex(other_model)
        self.assertEqual(self.dialog.preset_box.currentData(), "")

    def test_manually_choosing_a_presets_exact_pair_shows_that_preset(self) -> None:
        _id, _label, model_id, effort = next(p for p in PRESETS if p[0] == "smartest")
        self.dialog.model_box.setCurrentIndex(self.dialog.model_box.findData(model_id))
        self.dialog.effort_box.setCurrentIndex(self.dialog.effort_box.findData(effort))
        self.assertEqual(self.dialog.preset_box.currentData(), "smartest")


if __name__ == "__main__":
    unittest.main()
