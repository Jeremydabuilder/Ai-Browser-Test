"""PyBrowser's look: one stylesheet, generated from one small palette.

Why a stylesheet at all
-----------------------
Qt's default widget chrome is why a hand-built browser reads as a prototype:
the toolbar looks like a toolbar, the tabs look like a dialog's tabs, and
nothing says which application you are in. This file gives PyBrowser its own
identity - quiet, with one accent colour that appears in the same places every
time (the active tab, focus rings, the AI panel) rather than being sprinkled
around.

It is deliberately not a copy of any other browser's chrome.

Light and dark
--------------
Qt stylesheets have no `prefers-color-scheme`, so the theme is chosen once from
the palette the desktop gives us and the same token names are filled in with
different values. Every colour below comes from `Palette`; nothing is written
as a literal in the stylesheet itself, which is what keeps the two themes from
drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QPalette

#: The accent. Shared with the new-tab page (app/browser/newtab.py), because
#: the page inside the browser and the chrome around it should look related.
ACCENT_LIGHT = "#4b46d4"
ACCENT_DARK = "#8b86ff"


@dataclass(frozen=True)
class Palette:
    bg: str            # window background, behind everything
    surface: str       # raised things: the toolbar, the address bar
    surface_alt: str   # the tab strip, hover states
    line: str          # borders and separators
    text: str
    muted: str         # secondary text
    accent: str
    accent_soft: str   # accent at low intensity, for fills
    danger: str


LIGHT = Palette(
    bg="#f2f2f5", surface="#ffffff", surface_alt="#e9e9ef", line="#dcdce4",
    text="#1b1b21", muted="#6c6c7a", accent=ACCENT_LIGHT, accent_soft="#eeedfc",
    danger="#b3261e",
)

DARK = Palette(
    bg="#17171c", surface="#1f1f26", surface_alt="#26262f", line="#33333d",
    text="#f0f0f4", muted="#9a9aab", accent=ACCENT_DARK, accent_soft="#26253a",
    danger="#f2b8b5",
)


def palette_for(app) -> Palette:
    """Follow the desktop's light/dark preference.

    Read from the application palette rather than any Qt version-specific
    colour-scheme API, so this works the same on every platform Qt supports.
    """
    try:
        window = app.palette().color(QPalette.ColorRole.Window)
        return DARK if window.lightness() < 128 else LIGHT
    except Exception:  # noqa: BLE001 - a theme must never stop the app starting
        return LIGHT


def stylesheet(palette: Palette) -> str:
    """The whole application's styling, as one Qt stylesheet."""
    p = palette
    return f"""
    QMainWindow, QDialog {{ background: {p.bg}; }}
    QWidget {{ color: {p.text}; }}

    /* -- the toolbar ------------------------------------------------- */
    QToolBar {{
        background: {p.bg};
        border: none;
        border-bottom: 1px solid {p.line};
        padding: 5px 8px;
        spacing: 2px;
    }}
    QToolBar QToolButton {{
        background: transparent;
        border: none;
        border-radius: 7px;
        padding: 6px;
        margin: 0 1px;
    }}
    QToolBar QToolButton:hover {{ background: {p.surface_alt}; }}
    QToolBar QToolButton:pressed {{ background: {p.line}; }}
    QToolBar QToolButton:disabled {{ opacity: .4; }}
    QToolBar QToolButton:checked {{ background: {p.accent_soft}; }}

    /* -- the address bar --------------------------------------------- */
    QLineEdit {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: 9px;
        padding: 6px 11px;
        selection-background-color: {p.accent};
        selection-color: #ffffff;
    }}
    QLineEdit:focus {{ border-color: {p.accent}; }}
    QLineEdit:disabled {{ color: {p.muted}; }}

    /* -- tabs --------------------------------------------------------- */
    QTabWidget::pane {{ border: none; background: {p.bg}; }}
    QTabBar {{ background: {p.bg}; qproperty-drawBase: 0; }}
    QTabBar::tab {{
        background: transparent;
        color: {p.muted};
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 7px 12px;
        margin: 4px 2px 4px 0;
        min-width: 108px;
        max-width: 230px;
    }}
    QTabBar::tab:hover {{ background: {p.surface_alt}; color: {p.text}; }}
    /* The active tab is the one thing on screen that must be unmistakable. */
    QTabBar::tab:selected {{
        background: {p.surface};
        color: {p.text};
        border-color: {p.line};
        font-weight: 600;
    }}
    QTabBar::close-button {{
        subcontrol-position: right;
        margin-left: 4px;
    }}

    /* -- buttons ------------------------------------------------------ */
    QPushButton {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: 8px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{ border-color: {p.accent}; }}
    QPushButton:pressed {{ background: {p.surface_alt}; }}
    QPushButton:disabled {{ color: {p.muted}; border-color: {p.line}; }}
    QPushButton:default {{
        background: {p.accent};
        border-color: {p.accent};
        color: #ffffff;
    }}

    /* -- lists and trees ---------------------------------------------- */
    QTreeWidget, QListWidget, QTextBrowser, QPlainTextEdit {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: 9px;
        padding: 2px;
    }}
    QTreeWidget::item {{ padding: 4px 2px; border-radius: 5px; }}
    QTreeWidget::item:selected, QListWidget::item:selected {{
        background: {p.accent_soft};
        color: {p.text};
    }}
    QHeaderView::section {{
        background: {p.bg};
        border: none;
        border-bottom: 1px solid {p.line};
        padding: 5px 6px;
        color: {p.muted};
        font-weight: 600;
    }}

    /* -- chrome ------------------------------------------------------- */
    QStatusBar {{ background: {p.bg}; border-top: 1px solid {p.line}; color: {p.muted}; }}
    QStatusBar::item {{ border: none; }}
    QMenuBar {{ background: {p.bg}; border-bottom: 1px solid {p.line}; }}
    QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; background: transparent; }}
    QMenuBar::item:selected {{ background: {p.surface_alt}; }}
    QMenu {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: 9px;
        padding: 5px;
    }}
    QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {p.accent_soft}; }}
    QMenu::separator {{ height: 1px; background: {p.line}; margin: 5px 8px; }}

    QProgressBar {{
        background: {p.surface_alt};
        border: none;
        border-radius: 3px;
        height: 5px;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}

    QSplitter::handle {{ background: {p.line}; width: 1px; }}
    QSplitter::handle:hover {{ background: {p.accent}; }}

    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {p.line}; border-radius: 5px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.muted}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; }}
    QScrollBar::handle:horizontal {{
        background: {p.line}; border-radius: 5px; min-width: 28px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QRadioButton, QCheckBox {{ padding: 3px 0; }}
    QComboBox {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: 8px;
        padding: 6px 10px;
    }}
    QComboBox:hover {{ border-color: {p.accent}; }}
    QComboBox QAbstractItemView {{
        background: {p.surface};
        border: 1px solid {p.line};
        selection-background-color: {p.accent_soft};
        selection-color: {p.text};
    }}
    QToolTip {{
        background: {p.surface};
        color: {p.text};
        border: 1px solid {p.line};
        padding: 4px 7px;
    }}
    """


def apply(app) -> Palette:
    """Style the application. Returns the palette in use, for widgets that
    need one of its colours directly."""
    palette = palette_for(app)
    try:
        app.setStyleSheet(stylesheet(palette))
    except Exception:  # noqa: BLE001
        pass
    return palette
