"""Show candidate Py artwork at the sizes it is actually used at.

    python scripts/art_preview.py CANDIDATE_DIR [-o sheet.png]

A 2048-pixel render tells you nothing about whether the panel art survives the
44 pixels it is displayed at, or whether the fur reads against a dark page.
This lays every candidate out at its real sizes, on both grounds, so the
question can be answered by looking rather than by hoping.

Reads whatever is in the directory - full-body files, panel files, or one image
- and needs no naming convention beyond the state appearing in the filename.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui import theme  # noqa: E402
from app.ui.mascot import ALL_STATES  # noqa: E402
from app.ui.theme import METRICS  # noqa: E402

READABLE = (".png", ".webp", ".jpg", ".jpeg", ".gif", ".svg")

LIGHT, DARK = "#F7F7F9", "#14141C"


def find(directory: str) -> dict[str, dict[str, str]]:
    """Map state -> {full, panel} from whatever the directory holds."""
    found: dict[str, dict[str, str]] = {}
    for name in sorted(os.listdir(directory)):
        stem, extension = os.path.splitext(name)
        if extension.lower() not in READABLE:
            continue
        lower = stem.lower()
        state = next((s for s in ALL_STATES if s in lower), None)
        if state is None:
            continue
        variant = "panel" if "panel" in lower or "bust" in lower else "full"
        found.setdefault(state, {})[variant] = os.path.join(directory, name)
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("-o", "--out", default="py-art-preview.png")
    args = parser.parse_args()

    app = QApplication.instance() or QApplication([])  # noqa: F841
    theme.apply(app)
    found = find(args.directory)
    if not found:
        raise SystemExit(f"no state artwork recognised in {args.directory}")
    states = [state for state in ALL_STATES if state in found]

    panel = METRICS.mascot_panel          # 44 in the agent panel
    narrow = METRICS.mascot_panel_small   # 34 when it is narrow
    column = 168
    width = max(760, 60 + column * len(states))
    image = QImage(width, 1000, QImage.Format.Format_ARGB32)
    image.fill(QColor(LIGHT))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    def label(text, x, y, size, bold=False, colour="#1E2430"):
        # Runs to the edge of the sheet rather than a fixed box: a heading long
        # enough to matter was being clipped mid-word.
        font = QFont()
        font.setPointSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QColor(colour))
        painter.drawText(QRectF(x, y, width - x - 20, 24),
                         int(Qt.AlignmentFlag.AlignLeft), text)

    def draw(path, rect):
        if path:
            painter.drawImage(rect, QImage(path))

    accent = theme.ACCENT_LIGHT
    label("Py artwork — at the sizes it is actually used", 30, 16, 15, True)
    label(f"{args.directory}", 30, 44, 8, colour="#5A6070")

    label("FULL-BODY on the new-tab page — 210px tall", 30, 78, 9, True, accent)
    for index, state in enumerate(states):
        draw(found[state].get("full"),
             QRectF(30 + index * column, 100, column - 24, 210))
        label(state.upper(), 30 + index * column, 318, 8, True)

    label("PANEL — as delivered", 30, 352, 9, True, accent)
    for index, state in enumerate(states):
        draw(found[state].get("panel") or found[state].get("full"),
             QRectF(30 + index * column, 374, 140, 140))

    y = 546
    for ground, name in ((LIGHT, "light"), (DARK, "dark")):
        label(f"PANEL AT REAL SIZE on {name} — {panel}px, then {narrow}px",
              30, y, 9, True, accent)
        band = QRectF(24, y + 22, width - 48, panel + narrow + 26)
        painter.fillRect(band, QColor(ground))
        for index, state in enumerate(states):
            path = found[state].get("panel") or found[state].get("full")
            draw(path, QRectF(36 + index * (panel + 26), y + 30, panel, panel))
            draw(path, QRectF(36 + index * (panel + 26), y + 36 + panel, narrow, narrow))
        y += int(band.height()) + 46

    label("If a state is unreadable at these sizes it is unreadable in the app. "
          "The ears, the eyes and the prop are what have to survive.",
          30, y + 4, 8, False, "#5A6070")

    painter.end()
    image.copy(0, 0, width, min(1000, y + 40)).save(args.out, "PNG")
    print(f"wrote {args.out}")
    print(f"states shown: {', '.join(states)}")
    missing = [state for state in ALL_STATES if state not in found]
    if missing:
        print(f"not supplied: {', '.join(missing)} (these fall back to idle)")
    for state in states:
        if "panel" not in found[state]:
            print(f"note: {state} has no panel file; the full-body art is being "
                  "shown in its place, which is what 44px would actually get")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
