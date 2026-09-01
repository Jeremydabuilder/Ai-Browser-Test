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


# ---------------------------------------------------------------------------
# The design system
# ---------------------------------------------------------------------------
#
# One set of numbers, used by the stylesheet below AND by the widget code that
# cannot be styled in CSS (icon sizes, panel widths, the height a text box
# grows to). Before this existed, the same idea was spelled 8px in one file, 10
# in another and 9 in a third, which is most of what makes an interface look
# assembled rather than designed.


@dataclass(frozen=True)
class Metrics:
    """Spacing, size and shape. Every number in the UI should come from here."""

    # A 4px spacing scale. Anything not on it looks like a mistake, because it
    # usually is one.
    space_1: int = 4
    space_2: int = 8
    space_3: int = 12
    space_4: int = 16
    space_5: int = 24
    space_6: int = 32

    # Corner radii, from tightest to loosest. Three, not seven.
    radius_sm: int = 6      # icon buttons, list rows, tags
    radius_md: int = 9      # inputs, buttons, tabs
    radius_lg: int = 12     # cards and panels

    # Control heights. A browser feels cramped or clumsy mostly through these.
    control: int = 32       # buttons, the address bar
    control_sm: int = 26    # compact buttons: quick actions, find bar
    tab: int = 34
    icon_button: int = 30

    # Icon sizes, kept to two.
    icon: int = 18
    icon_sm: int = 14

    # Type scale.
    text: int = 13
    text_sm: int = 12
    text_xs: int = 11
    text_lg: int = 15

    # The AI panel: wide enough to read, never more than a third of the window.
    panel_default: int = 380
    panel_min: int = 300
    panel_max_share: float = 0.42

    # Tabs.
    tab_min_width: int = 120
    tab_max_width: int = 220

    # Py. Big enough to read an expression, small enough to stay furniture.
    mascot_panel: int = 40
    mascot_panel_small: int = 30   # when the panel is narrow
    mascot_newtab: int = 56


METRICS = Metrics()

#: The accent. Shared with the new-tab page (app/browser/newtab.py), because
#: the page inside the browser and the chrome around it should look related.
ACCENT_LIGHT = "#4b46d4"
ACCENT_DARK = "#8b86ff"


@dataclass(frozen=True)
class Palette:
    """Colour by role, never by name.

    Nothing in the UI asks for "the grey one"; it asks for a surface, a line, a
    muted text. That is what lets the dark theme be a different set of values
    rather than a different set of rules.
    """

    bg: str             # window background, behind everything
    surface: str        # raised things: the address bar, cards, the active tab
    surface_alt: str    # recessed things: inactive tabs, hover on the toolbar
    surface_hover: str  # hover on a surface
    line: str           # ordinary borders and separators
    line_strong: str    # a border that needs to be seen: hover, scrollbars
    text: str
    muted: str          # secondary text
    disabled: str       # text on a control that cannot be used
    accent: str
    accent_hover: str
    accent_soft: str    # accent at low intensity, for fills
    #: Text drawn ON the accent. White fails against the lighter accent the
    #: dark theme needs, so this is a palette entry rather than a constant.
    accent_text: str
    danger: str
    danger_soft: str
    warning: str        # the approval prompt, which is a caution and not an error
    warning_soft: str
    warning_text: str
    success: str
    tooltip_bg: str
    tooltip_text: str


LIGHT = Palette(
    bg="#f4f4f7", surface="#ffffff", surface_alt="#eaeaf0", surface_hover="#f0f0f5",
    line="#e0e0e8", line_strong="#c9c9d4",
    text="#17171d", muted="#65656f", disabled="#a8a8b4",
    accent=ACCENT_LIGHT, accent_hover="#3f3ac2", accent_soft="#eeedfc",
    accent_text="#ffffff",
    danger="#b3261e", danger_soft="#fdeceb",
    warning="#a97400", warning_soft="#fff8e6", warning_text="#5c3d00",
    success="#2e7d32",
    tooltip_bg="#2a2a33", tooltip_text="#f4f4f7",
)

DARK = Palette(
    bg="#141419", surface="#1e1e25", surface_alt="#262630", surface_hover="#2c2c38",
    line="#30303b", line_strong="#43434f",
    text="#eeeef3", muted="#9797a6", disabled="#61616e",
    accent=ACCENT_DARK, accent_hover="#a29dff", accent_soft="#282740",
    accent_text="#16162a",
    danger="#f2b8b5", danger_soft="#3a2422",
    warning="#e0b661", warning_soft="#332a17", warning_text="#f0dcb4",
    success="#7bc47f",
    tooltip_bg="#33333f", tooltip_text="#eeeef3",
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


def stylesheet(palette: Palette, m: Metrics = METRICS) -> str:
    """The whole application's styling, as one Qt stylesheet.

    Every colour comes from `palette` and every measurement from `m`, so there
    is one place to change how the browser looks and no way for two controls to
    disagree by four pixels.
    """
    p = palette
    return f"""
    QMainWindow, QDialog {{ background: {p.bg}; }}
    QWidget {{ color: {p.text}; font-size: {m.text}px; }}

    /* -- the toolbar ------------------------------------------------- */
    QToolBar {{
        background: {p.bg};
        border: none;
        padding: {m.space_2}px {m.space_3}px {m.space_1}px;
        spacing: {m.space_1}px;
    }}
    QToolBar QToolButton {{
        background: transparent;
        border: none;
        border-radius: {m.radius_sm}px;
        min-width: {m.icon_button}px;
        min-height: {m.icon_button}px;
        padding: 0;
    }}
    QToolBar QToolButton:hover {{ background: {p.surface_alt}; }}
    QToolBar QToolButton:pressed {{ background: {p.line}; }}
    QToolBar QToolButton:checked {{ background: {p.accent_soft}; }}

    /* -- inputs -------------------------------------------------------- */
    QLineEdit {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        min-height: {m.control}px;
        padding: 0 {m.space_3}px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QLineEdit:hover {{ border-color: {p.line_strong}; }}
    /* Focus is a ring, not a thicker border - a border that changes width
       makes the whole control shift by a pixel as you click into it. */
    QLineEdit:focus {{
        border-color: {p.accent};
        background: {p.surface};
    }}
    QLineEdit:disabled {{ color: {p.muted}; background: {p.surface_alt}; }}

    /* -- tabs ---------------------------------------------------------- */
    QTabWidget::pane {{ border: none; background: {p.bg}; }}
    QTabBar {{ background: {p.bg}; qproperty-drawBase: 0; }}
    QTabBar::tab {{
        background: {p.surface_alt};
        color: {p.muted};
        border: 1px solid transparent;
        border-radius: {m.radius_md}px;
        height: {m.tab}px;
        padding: 0 {m.space_2}px;
        margin: {m.space_1}px {m.space_1}px 0 0;
        min-width: {m.tab_min_width}px;
        max-width: {m.tab_max_width}px;
    }}
    QTabBar::tab:hover {{ background: {p.surface_hover}; color: {p.text}; }}
    /* The active tab is the one thing on screen that must be unmistakable:
       it is the only tab with the page's own background. */
    QTabBar::tab:selected {{
        background: {p.surface};
        color: {p.text};
        border-color: {p.line};
        font-weight: 600;
    }}
    QTabBar::close-button {{ subcontrol-position: right; }}
    QTabBar QToolButton {{
        background: {p.surface_alt};
        border: 1px solid {p.line};
        border-radius: {m.radius_sm}px;
        margin: {m.space_1}px 0;
    }}
    QTabBar QToolButton:hover {{ background: {p.surface_hover}; }}

    /* -- buttons -------------------------------------------------------- */
    QPushButton {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        min-height: {m.control}px;
        padding: 0 {m.space_4}px;
        color: {p.text};
    }}
    QPushButton:hover {{ background: {p.surface_hover}; border-color: {p.line_strong}; }}
    QPushButton:pressed {{ background: {p.surface_alt}; }}
    QPushButton:disabled {{ color: {p.disabled}; border-color: {p.line}; background: {p.bg}; }}
    QPushButton:focus {{ border-color: {p.accent}; }}
    QPushButton[kind="primary"] {{
        background: {p.accent}; border-color: {p.accent};
        color: {p.accent_text}; font-weight: 600;
    }}
    QPushButton[kind="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton[kind="primary"]:disabled {{
        background: {p.surface_alt}; border-color: {p.line}; color: {p.disabled};
    }}
    QPushButton[kind="quiet"] {{
        background: transparent; border-color: transparent; color: {p.muted};
        min-height: {m.control_sm}px; padding: 0 {m.space_2}px;
    }}
    QPushButton[kind="quiet"]:hover {{ background: {p.surface_alt}; color: {p.text}; }}
    QPushButton[kind="chip"] {{
        min-height: {m.control_sm}px; padding: 0 {m.space_3}px;
        border-radius: {m.control_sm // 2}px; color: {p.muted};
        background: {p.surface};
    }}
    QPushButton[kind="chip"]:hover {{ color: {p.accent}; border-color: {p.accent}; }}
    QPushButton[kind="danger"] {{ color: {p.danger}; }}
    QPushButton[kind="danger"]:hover {{ border-color: {p.danger}; background: {p.danger_soft}; }}

    /* -- lists, trees, text ---------------------------------------------- */
    QTreeWidget, QListWidget {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        padding: {m.space_1}px;
    }}
    QTextBrowser, QPlainTextEdit {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        padding: {m.space_2}px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QTextBrowser[kind="flat"] {{
        background: transparent; border: none; padding: 0;
    }}
    QTreeWidget::item {{ padding: {m.space_1}px 2px; border-radius: {m.radius_sm}px; }}
    QTreeWidget::item:selected, QListWidget::item:selected {{
        background: {p.accent_soft}; color: {p.text};
    }}
    QHeaderView::section {{
        background: {p.bg};
        border: none;
        border-bottom: 1px solid {p.line};
        padding: {m.space_1}px {m.space_2}px;
        color: {p.muted};
        font-weight: 600;
        font-size: {m.text_sm}px;
    }}

    /* -- chrome ---------------------------------------------------------- */
    QStatusBar {{
        background: {p.bg}; border-top: 1px solid {p.line};
        color: {p.muted}; font-size: {m.text_sm}px;
    }}
    QStatusBar::item {{ border: none; }}
    QMenuBar {{ background: {p.bg}; padding: 2px {m.space_2}px; }}
    QMenuBar::item {{
        padding: {m.space_1}px {m.space_2}px; border-radius: {m.radius_sm}px;
        background: transparent;
    }}
    QMenuBar::item:selected {{ background: {p.surface_alt}; }}
    QMenu {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        padding: {m.space_1}px;
    }}
    QMenu::item {{
        padding: {m.space_2}px {m.space_5}px {m.space_2}px {m.space_3}px;
        border-radius: {m.radius_sm}px;
    }}
    QMenu::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}
    QMenu::separator {{ height: 1px; background: {p.line}; margin: {m.space_1}px {m.space_2}px; }}

    QProgressBar {{
        background: {p.surface_alt}; border: none;
        border-radius: 2px; height: 4px;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 2px; }}

    QSplitter::handle {{ background: {p.line}; width: 1px; }}
    QSplitter::handle:hover {{ background: {p.accent}; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {p.line_strong}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.muted}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{
        background: {p.line_strong}; border-radius: 5px; min-width: 30px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QRadioButton, QCheckBox {{ padding: {m.space_1}px 0; spacing: {m.space_2}px; }}
    QComboBox {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        min-height: {m.control}px;
        padding: 0 {m.space_3}px;
    }}
    QComboBox:hover {{ border-color: {p.line_strong}; }}
    QComboBox:focus {{ border-color: {p.accent}; }}
    QComboBox::drop-down {{ border: none; width: {m.space_5}px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface};
        border: 1px solid {p.line};
        border-radius: {m.radius_md}px;
        padding: {m.space_1}px;
        selection-background-color: {p.accent_soft};
        selection-color: {p.text};
    }}
    QToolTip {{
        background: {p.tooltip_bg};
        color: {p.tooltip_text};
        border: none;
        border-radius: {m.radius_sm}px;
        padding: {m.space_1}px {m.space_2}px;
    }}
    QLabel[kind="muted"] {{ color: {p.muted}; font-size: {m.text_sm}px; }}
    QLabel[kind="section"] {{
        color: {p.muted}; font-size: {m.text_xs}px; font-weight: 600;
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
