"""Everything that wraps Qt WebEngine: the profile, the page, the view, tabs."""

from app.browser.controller import (
    BrowserController,
    PageElement,
    PageStructure,
    ScrollDirection,
)
from app.browser.load_error import ErrorCategory, LoadError
from app.browser.profile import BrowserProfile
from app.browser.tab import BrowserTab
from app.browser.tab_manager import TabManager
from app.browser.web_page import BrowserPage

__all__ = [
    "BrowserController",
    "BrowserPage",
    "BrowserProfile",
    "BrowserTab",
    "ErrorCategory",
    "LoadError",
    "PageElement",
    "PageStructure",
    "ScrollDirection",
    "TabManager",
]
