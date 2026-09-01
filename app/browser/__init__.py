"""Everything that wraps Qt WebEngine: the profile, the page, tabs, and the
programmatic control surface (BrowserController)."""

from app.browser.newtab import NEW_TAB_URL, register_scheme

# Chromium reads its URL-scheme registry exactly once, before QApplication is
# constructed, so pybrowser:// has to be declared at import time rather than
# when the first profile is built. Importing anything from this package is
# early enough; the call is idempotent.
register_scheme()

from app.browser.controller import (  # noqa: E402 - must follow the line above
    BrowserController,
    Heading,
    PageElement,
    PageForm,
    PageStructure,
    ScrollDirection,
)
from app.browser.futures import BrowserFuture
from app.browser.load_error import ErrorCategory, LoadError
from app.browser.profile import BrowserProfile
from app.browser.results import (
    ActionError,
    ActionResult,
    Effects,
    ElementRef,
    ErrorCode,
    PageState,
)
from app.browser.safety import Sensitivity, SensitivityAssessment
from app.browser.tab import BrowserTab
from app.browser.tab_manager import TabManager
from app.browser.web_page import BrowserPage

__all__ = [
    "NEW_TAB_URL",
    "ActionError",
    "ActionResult",
    "BrowserController",
    "BrowserFuture",
    "BrowserPage",
    "BrowserProfile",
    "BrowserTab",
    "Effects",
    "ElementRef",
    "ErrorCategory",
    "ErrorCode",
    "Heading",
    "LoadError",
    "PageElement",
    "PageForm",
    "PageState",
    "PageStructure",
    "ScrollDirection",
    "Sensitivity",
    "SensitivityAssessment",
    "TabManager",
]
