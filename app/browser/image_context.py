"""Image context: a screenshot of the current page, or a local image file
the user explicitly picked - never anything captured or read without that
explicit action.

Bytes only leave this process when the user has been told which provider
receives them (see MainWindow's disclosure prompt) - nothing here uploads
anything itself.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

#: Matches the local-file size story: fail fast, before any encoding work.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
SUPPORTED_IMAGE_SUFFIXES = frozenset(_MIME_BY_SUFFIX)


class ImageContextError(RuntimeError):
    """Always carries a message safe to show the person who chose the image."""


@dataclass(frozen=True)
class ImageAttachment:
    source: str          # "screenshot:<url>" or the file path
    filename: str
    mime_type: str
    data: bytes

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


def read_local_image(path: str) -> ImageAttachment:
    """Read an image file the user explicitly picked. Raises
    ImageContextError for an unsupported extension, an unreadable path, or
    a file too large."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    mime_type = _MIME_BY_SUFFIX.get(suffix)
    if mime_type is None:
        raise ImageContextError(
            f"'{suffix or file_path.name}' is not a supported image type. "
            f"Supported: {', '.join(sorted(SUPPORTED_IMAGE_SUFFIXES))}.")
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise ImageContextError(f"Could not read that image: {exc}") from exc
    if size > MAX_IMAGE_BYTES:
        raise ImageContextError(
            f"That image is too large ({size // (1024 * 1024)} MB). "
            f"The limit is {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise ImageContextError(f"Could not read that image: {exc}") from exc
    return ImageAttachment(source=str(file_path), filename=file_path.name,
                           mime_type=mime_type, data=data)


def screenshot_attachment(pixmap, *, source: str) -> ImageAttachment:
    """Turn a captured QPixmap into an ImageAttachment, PNG-encoded.

    Takes the pixmap rather than a tab so this module stays free of any Qt
    widget/window dependency beyond QByteArray/QBuffer - the same boundary
    BrowserController already keeps between Qt and plain data.
    """
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    buffer_bytes = QByteArray()
    buffer = QBuffer(buffer_bytes)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pixmap.save(buffer, "PNG"):
        raise ImageContextError("Could not encode the screenshot.")
    buffer.close()
    return ImageAttachment(source=source, filename="screenshot.png",
                           mime_type="image/png", data=bytes(buffer_bytes.data()))
