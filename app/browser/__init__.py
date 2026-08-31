"""Everything that wraps Qt WebEngine: the profile, the page, the view, tabs."""

from app.browser.profile import BrowserProfile
from app.browser.web_page import BrowserPage
from app.browser.tab import BrowserTab
from app.browser.tab_manager import TabManager

__all__ = ["BrowserProfile", "BrowserPage", "BrowserTab", "TabManager"]
