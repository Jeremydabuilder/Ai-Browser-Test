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


#: Verbs that open a goal rather than name a thing to look up - "find the
#: cheapest flight" is a task, "cheapest flights nyc" is a search, and the
#: difference is almost always which one of these starts the sentence.
_TASK_VERBS = re.compile(
    r"^(find|compare|research|plan|summari[sz]e|explore|look into|check|"
    r"figure out|help me)\b", re.IGNORECASE
)


def looks_like_a_task(text: str) -> bool:
    """True when this reads as a goal for Py rather than a search or a URL.

    Deliberately conservative - a false positive here would put an "Ask Py"
    icon next to an ordinary search, which is noise, while a false negative
    just means the address bar behaves exactly as it always did. Two
    independent signs: the sentence opens with a task verb ("find...",
    "compare..."), or it is simply long enough that it reads as an
    instruction rather than a handful of keywords - five words is roughly
    where "best budget noise cancelling headphones" (a search) gives way to
    "find me noise cancelling headphones under $150 with good bass" (a task).
    """
    text = (text or "").strip()
    if not text or not is_probably_search(text):
        return False
    if _TASK_VERBS.match(text):
        return True
    return len(text.split()) >= 8


def display_text(url: QUrl) -> str:
    """What to show in the address bar for a loaded URL.

    Blank for a new tab: `pybrowser://newtab/` is browser UI, not a place you
    went, and showing its address would only invite the user to select and
    delete it before typing.

    Other internal pages are shown. The Mission Library *is* somewhere you
    went - it has a URL, it works with back and forward, and hiding its address
    would make navigating to a mission feel like nothing happened.
    """
    if url.isEmpty() or url.toString() == "about:blank":
        return ""
    if url.scheme() == "pybrowser" and url.host() == "newtab":
        return ""
    return url.toString()


def short_host(url: QUrl) -> str:
    host = url.host()
    return host[4:] if host.startswith("www.") else host
