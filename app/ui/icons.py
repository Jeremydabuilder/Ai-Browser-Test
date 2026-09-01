"""PyBrowser's own toolbar icons.

The toolbar previously asked the desktop for icons by name (`go-previous`,
`bookmark-new`) and fell back to Qt's built-in style icons. That produces a
different-looking browser on every machine, and on a system with no icon theme
the fallbacks are actively wrong: the bookmark button rendered as a floppy
disk and Home as a folder.

So the icons are drawn here instead - a handful of tiny SVGs, one stroke
weight, one corner style. They are recoloured to match the current theme, so
they work in light and dark without a second set of files.

SVG rather than QPainter calls because the shapes stay readable as shapes, and
QtSvg ships with PySide6 - no new dependency.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

#: 24x24 viewBox, 2px strokes, round caps. `currentColor` is substituted with
#: the theme colour before rendering.
_SHAPES: dict[str, str] = {
    "back": '<path d="M15 5 8 12l7 7"/>',
    "forward": '<path d="M9 5l7 7-7 7"/>',
    "reload": ('<path d="M20 12a8 8 0 1 1-2.3-5.6"/>'
               '<path d="M20 4v5h-5"/>'),
    "stop": '<path d="M7 7l10 10M17 7L7 17"/>',
    "home": ('<path d="M4 11.5 12 5l8 6.5"/>'
             '<path d="M6.5 10.5V19h11v-8.5"/>'),
    "star": '<path d="M12 4.6l2.3 4.9 5.2.7-3.8 3.7.9 5.3-4.6-2.5-4.6 2.5.9-5.3L4.5 10.2l5.2-.7L12 4.6Z"/>',
    "download": ('<path d="M12 4v10"/><path d="M8 10.5l4 4 4-4"/>'
                 '<path d="M5 18.5h14"/>'),
    "sparkle": ('<path d="M12 4l1.7 4.6L18 10.3l-4.3 1.7L12 16.6l-1.7-4.6L6 10.3l4.3-1.7L12 4Z" '
                'fill="currentColor" stroke="none"/>'),
    "close": '<path d="M8 8l8 8M16 8l-8 8"/>',
    "plus": '<path d="M12 6v12M6 12h12"/>',
    "up": '<path d="M6 15l6-6 6 6"/>',
    "down": '<path d="M6 9l6 6 6-6"/>',
}

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="{weight}" stroke-linecap="round" '
    'stroke-linejoin="round">{shape}</svg>'
)


def _render(shape: str, color: str, size: int, weight: float, fill: bool) -> QPixmap:
    body = shape
    if fill and "fill=" not in shape:
        body = shape.replace("<path ", f'<path fill="{color}" ')
    svg = _TEMPLATE.format(color=color, weight=weight, shape=body)
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def icon(name: str, color: str, *, size: int = 40, weight: float = 2.0,
         disabled_color: str | None = None, filled: bool = False) -> QIcon:
    """One icon, in the theme's colours.

    Rendered larger than it is displayed so it stays crisp on a high-DPI
    screen, and given an explicit Disabled pixmap - Qt's automatic greying of a
    monochrome icon is nearly invisible, which is what made the disabled Back
    button look enabled.
    """
    result = QIcon()
    shape = _SHAPES.get(name)
    if shape is None:
        return result
    result.addPixmap(_render(shape, color, size, weight, filled), QIcon.Mode.Normal)
    if disabled_color:
        result.addPixmap(_render(shape, disabled_color, size, weight, filled),
                         QIcon.Mode.Disabled)
    return result


def available() -> tuple[str, ...]:
    return tuple(_SHAPES)
