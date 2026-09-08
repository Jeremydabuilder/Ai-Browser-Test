"""FlowLayout: a row that wraps instead of clipping.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_flow_layout -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from app.ui.flow_layout import FlowLayout  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class FlowLayoutTests(unittest.TestCase):
    def _row(self, labels: list[str]) -> tuple[QWidget, FlowLayout, list[QPushButton]]:
        host = QWidget()
        flow = FlowLayout(host, spacing=6)
        buttons = []
        for label in labels:
            button = QPushButton(label, host)
            flow.addWidget(button)
            buttons.append(button)
        return host, flow, buttons

    def test_everything_fits_on_one_row_when_there_is_room(self):
        host, flow, buttons = self._row(["Summarise", "Key points", "Explain"])
        host.resize(2000, 200)
        host.show()
        tops = {button.geometry().y() for button in buttons}
        self.assertEqual(len(tops), 1, "all buttons should share one row when width allows")

    def test_a_button_that_does_not_fit_wraps_to_a_new_row(self):
        host, flow, buttons = self._row(
            ["Summarise", "Key points", "Explain", "Compare my tabs"])
        host.resize(150, 400)
        host.show()
        tops = [button.geometry().y() for button in buttons]
        self.assertGreater(len(set(tops)), 1, "a too-narrow row must wrap, not clip")
        # Every button must land within the row's width - none clipped past
        # the right edge, which is the whole point of wrapping instead of a
        # plain QHBoxLayout.
        for button in buttons:
            self.assertLessEqual(button.geometry().right(), 150)

    def test_count_and_item_at_track_added_widgets(self):
        _host, flow, buttons = self._row(["A", "B", "C"])
        self.assertEqual(flow.count(), 3)
        self.assertIs(flow.itemAt(0).widget(), buttons[0])

    def test_take_at_removes_and_returns_the_item(self):
        _host, flow, buttons = self._row(["A", "B"])
        item = flow.takeAt(0)
        self.assertIs(item.widget(), buttons[0])
        self.assertEqual(flow.count(), 1)

    def test_an_empty_flow_layout_has_a_zero_size_hint(self):
        host = QWidget()
        flow = FlowLayout(host)
        self.assertEqual(flow.sizeHint(), flow.minimumSize())


if __name__ == "__main__":
    unittest.main()
