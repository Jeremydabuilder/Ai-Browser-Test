"""Everything that wraps Qt WebEngine: the profile, the page, tabs, and the
programmatic control surface (BrowserController)."""

from app.browser.controller import (
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
