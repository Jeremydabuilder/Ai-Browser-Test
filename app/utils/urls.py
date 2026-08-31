"""Turning whatever the user typed into a QUrl.

This is the piece that makes an address bar feel like a browser: "github.com",
"localhost:8000", "/etc/hosts" and "cheap laptops" all have to do the right
thing.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from PySide6.QtCore import QUrl

# Anything that already looks like scheme://... is taken at face value.
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# host[:port][/path] where host has a dot and no spaces -> treat as a URL.
_LOOKS_LIKE_HOST = re.compile(
    r"^(?:[\w-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:[/?#]\S*)?$"
)
_LOCALHOST = re.compile(r"^localhost(?::\d+)?(?:[/?#]\S*)?$", re.IGNORECASE)
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:[/?#]\S*)?$")

SCHEMES_WITHOUT_HOST = ("about:", "data:", "javascript:", "mailto:", "chrome:")

# Loopback and RFC1918 addresses are almost always a local dev server speaking
# plain HTTP, so defaulting them to https just produces a handshake error.
# Chrome behaves the same way.
_PRIVATE_HOST = re.compile(
    r"^(?:localhost|127(?:\.\d{1,3}){3}|\[?::1\]?|0\.0\.0\.0"
    r"|10(?:\.\d{1,3}){3}"
    r"|192\.168(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
    r"(?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _default_scheme(host_and_rest: str) -> str:
    return "http://" if _PRIVATE_HOST.match(host_and_rest) else "https://"


def normalize(text: str, search_template: str) -> QUrl:
    """Return the QUrl to navigate to for the raw ``text`` in the address bar.

    ``search_template`` is a format string containing ``{query}``; it is used
    when the text is not recognisable as an address.
    """
    text = (text or "").strip()
    if not text:
        return QUrl("about:blank")

    # An existing local file path is a legitimate thing to open.
    if os.path.exists(os.path.expanduser(text)):
        return QUrl.fromLocalFile(os.path.abspath(os.path.expanduser(text)))

    # A scheme only counts if it is followed by "//" or is a known hostless
    # scheme. Without this check "localhost:8000" parses as scheme "localhost".
    if _HAS_SCHEME.match(text) and (
        "://" in text or text.lower().startswith(SCHEMES_WITHOUT_HOST)
    ):
        url = QUrl(text)
        if url.isValid():
            return url

    if _LOCALHOST.match(text) or _IPV4.match(text) or _LOOKS_LIKE_HOST.match(text):
        return QUrl(_default_scheme(text) + text)

    return QUrl(search_template.format(query=quote_plus(text)))


def is_probably_search(text: str) -> bool:
    """True when ``normalize`` would fall through to the search engine."""
    text = (text or "").strip()
    if not text:
        return False
    if _HAS_SCHEME.match(text) and (
        "://" in text or text.lower().startswith(SCHEMES_WITHOUT_HOST)
    ):
        return False
    return not (
        _LOCALHOST.match(text) or _IPV4.match(text) or _LOOKS_LIKE_HOST.match(text)
    )


def display_text(url: QUrl) -> str:
    """What to show in the address bar for a loaded URL."""
    if url.isEmpty() or url.toString() == "about:blank":
        return ""
    return url.toString()


def short_host(url: QUrl) -> str:
    host = url.host()
    return host[4:] if host.startswith("www.") else host
