"""Accessibility helpers: names and descriptions for assistive technology, and the audit that keeps them honest.

Qt exposes a widget to a screen reader through its *accessible name* (what it is) and *description* (what it
does). A button with text has a name already; an icon-only button, a search box with only placeholder text, a
list or a tree has none unless we set it. ``describe`` sets both explicitly. ``install_fallback`` is a safety
net, not the plan: when a widget appears with no name, it derives one from its own tooltip / placeholder (and
marks it ``a11yDerived`` so the audit can tell explicit names from guesses).

Everything here is deterministic and testable. What it can NOT tell you is how a screen reader actually
speaks the result - that needs NVDA / Narrator / VoiceOver on a real machine.
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QListWidget, QPlainTextEdit, QSlider, QTabBar,
    QTextBrowser, QTextEdit, QToolButton, QTreeWidget, QWidget, QDialog, QMainWindow,
)

#: widget classes a user can operate or read, which therefore need a name.
NAMED = (QAbstractButton, QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QListWidget, QTreeWidget, QSlider,
         QAbstractSpinBox, QTabBar, QTextBrowser)


def describe(widget: QWidget, name: str, description: str = "") -> None:
    """Give ``widget`` an accessible name (and optionally a longer description)."""
    widget.setAccessibleName(name)
    if description:
        widget.setAccessibleDescription(description)


def enable_clear(edit: QLineEdit, what: str = "text") -> None:
    """``setClearButtonEnabled(True)`` plus an accessible name for the clear button Qt adds (it has none)."""
    edit.setClearButtonEnabled(True)

    def name_buttons(*_args) -> None:
        for button in edit.findChildren(QToolButton):          # Qt creates the button lazily, so name it as it appears
            if button.metaObject().className() == "QLineEditIconButton" and not button.text() \
                    and button.accessibleName() != f"Clear {what}":
                describe(button, f"Clear {what}")

    edit.setProperty("a11yClear", what)
    name_buttons()
    edit.textChanged.connect(name_buttons)
    QTimer.singleShot(0, name_buttons)


def effective_name(widget: QWidget) -> str:
    """What assistive technology would announce for ``widget`` (name, else a button's own text)."""
    name = (widget.accessibleName() or "").strip()
    if name:
        return name
    if isinstance(widget, QAbstractButton):
        return widget.text().replace("&", "").strip()
    return ""


def is_internal(widget: QWidget) -> bool:
    """Qt's own plumbing (a spin box's inner editor, a scroll bar...) is named by its owner."""
    if widget.objectName().startswith("qt_"):
        return True
    parent = widget.parent()
    return isinstance(widget, QLineEdit) and isinstance(parent, (QAbstractSpinBox, QComboBox))


def missing_names(root: QWidget) -> list[tuple[str, str, str]]:
    """[(class, object name, hint)] for every operable widget under ``root`` that has no accessible name."""
    out = []
    for widget in [root, *root.findChildren(QWidget)]:
        if not isinstance(widget, NAMED) or is_internal(widget) or not widget.isVisibleTo(root):
            continue
        if not effective_name(widget):
            hint = (getattr(widget, "placeholderText", lambda: "")() or widget.toolTip() or "")[:60]
            out.append((type(widget).__name__, widget.objectName(), hint))
    return out


def _derive(widget: QWidget) -> str:
    if isinstance(widget, QLineEdit):
        return widget.placeholderText().strip().rstrip("\u2026.") or widget.toolTip().strip()
    if isinstance(widget, QToolButton) and widget.metaObject().className() == "QLineEditIconButton":
        owner = widget.parent()
        return f"Clear {owner.property('a11yClear') or 'text'}" if owner is not None else "Clear text"
    return widget.toolTip().strip()


def name_unnamed(root: QWidget) -> int:
    """Name every still-unnamed operable widget under ``root`` from its own tooltip/placeholder.
    Returns how many were named. Safe to call repeatedly; never overwrites an explicit name."""
    named = 0
    try:
        if not shiboken6.isValid(root):
            return 0
        for widget in [root, *root.findChildren(QWidget)]:
            if isinstance(widget, NAMED) and not is_internal(widget) and not effective_name(widget):
                derived = _derive(widget)
                if derived:
                    widget.setAccessibleName(derived)
                    widget.setProperty("a11yDerived", True)
                    named += 1
    except RuntimeError:                                  # a widget deleted mid-sweep
        pass
    return named


_installed = False


def install_fallback(app) -> None:
    """A safety net, installed once: sweep a window shortly after it is built, and again whenever keyboard focus
    moves into a window (so a panel added later is covered). Deliberately NOT an application-wide event filter:
    a Python filter on every event crashed Qt WebEngine pages in testing."""
    global _installed
    if _installed or app is None:
        return
    _installed = True

    def sweep_later(widget):
        QTimer.singleShot(0, lambda: name_unnamed(widget))

    for cls in (QDialog, QMainWindow):
        original = cls.__init__

        def patched(self, *args, __original=original, **kwargs):
            __original(self, *args, **kwargs)
            sweep_later(self)
        cls.__init__ = patched

    def on_focus(_old, new):
        if new is not None and shiboken6.isValid(new):
            name_unnamed(new.window())
    app.focusChanged.connect(on_focus)
