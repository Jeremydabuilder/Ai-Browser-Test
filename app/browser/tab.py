"""A single browser tab: a web view plus the state the UI needs to show it."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.browser.load_error import LoadError
from app.browser.profile import BrowserProfile
from app.browser.web_page import BrowserPage

MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld
ISOLATED_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld


class BrowserTab(QWidget):
    """Owns one QWebEngineView and re-broadcasts its signals as tab signals.

    The main window never talks to QWebEngineView directly. That indirection is
    what will later let the AI agent drive a tab through a small, stable API
    (``navigate``, ``run_javascript``, ``current_url``) instead of poking at Qt
    internals.
    """

    url_changed = Signal(QUrl)
    title_changed = Signal(str)
    icon_changed = Signal(QIcon)
    load_started = Signal()
    load_progress = Signal(int)
    load_finished = Signal(bool)
    link_hovered = Signal(str)
    # Emitted when this tab wants a sibling tab (target=_blank, window.open).
    new_tab_requested = Signal(object)  # payload: BrowserTab
    status_message = Signal(str)
    # A load failure, already translated into a human-readable message.
    load_error = Signal(object)  # payload: LoadError
    # An action requested by PyBrowser's own new-tab page.
    internal_action = Signal(str, dict)

    def __init__(
        self,
        profile: BrowserProfile,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._loading = False
        self._load_ok = True
        self._last_error: LoadError | None = None

        self._view = QWebEngineView(self)
        self._page = BrowserPage(profile.qt_profile, self._view)
        self._page.new_page_factory = self._create_page_for_new_window
        self._view.setPage(self._page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._view)

        self._connect_signals()

    # -- wiring ---------------------------------------------------------
    def _connect_signals(self) -> None:
        self._view.urlChanged.connect(self.url_changed)
        self._view.titleChanged.connect(self.title_changed)
        self._view.iconChanged.connect(self.icon_changed)
        self._view.loadStarted.connect(self._on_load_started)
        self._view.loadProgress.connect(self.load_progress)
        self._view.loadFinished.connect(self._on_load_finished)
        self._page.link_hovered_changed.connect(self.link_hovered)
        self._page.load_error.connect(self._on_load_error)
        self._page.certificate_rejected.connect(self._on_certificate_rejected)
        self._page.render_process_crashed.connect(self.status_message)
        self._page.internal_action.connect(self.internal_action)
        self._page.fullScreenRequested.connect(self._on_fullscreen_requested)

    def _on_load_started(self) -> None:
        self._loading = True
        self.load_started.emit()

    def _on_load_finished(self, ok: bool) -> None:
        self._loading = False
        self._load_ok = ok
        self.load_finished.emit(ok)

    def _on_load_error(self, error: LoadError) -> None:
        """Chromium renders its own error page; we add a readable explanation."""
        self._last_error = error
        self.status_message.emit(error.message)
        self.load_error.emit(error)

    def _on_certificate_rejected(self, host: str, description: str) -> None:
        # Phrased for a person, not for a TLS engineer, and it says plainly that
        # we blocked it - there is no click-through to offer.
        self.status_message.emit(
            f"Blocked a connection to {host}: its security certificate could not "
            f"be trusted ({description})."
        )

    def _create_page_for_new_window(self, window_type):
        """Build the page Chromium asked for, wrapped in a brand-new tab."""
        tab = BrowserTab(self._profile)
        self.new_tab_requested.emit(tab)
        return tab.page

    def _on_fullscreen_requested(self, request) -> None:
        """Honour a page's request to go fullscreen (e.g. a YouTube video)."""
        request.accept()
        window = self.window()
        if request.toggleOn():
            window.showFullScreen()
        else:
            window.showNormal()

    # -- accessors ------------------------------------------------------
    @property
    def view(self) -> QWebEngineView:
        return self._view

    @property
    def page(self) -> BrowserPage:
        return self._page

    @property
    def is_loading(self) -> bool:
        return self._loading

    @property
    def last_error(self) -> LoadError | None:
        """The most recent load failure, or None if the last load succeeded."""
        return self._last_error

    def url(self) -> QUrl:
        return self._view.url()

    def title(self) -> str:
        return self._view.title() or self.url().host() or "New Tab"

    def icon(self) -> QIcon:
        return self._view.icon()

    # -- navigation API (also the surface a future AI agent will use) ----
    def navigate(self, url: QUrl | str) -> bool:
        """Load ``url``. Returns False (and reports) if it is not usable.

        Guarding here means a malformed address produces a clear message
        instead of a silently blank page.
        """
        target = QUrl(url) if isinstance(url, str) else url
        if not self._is_navigable(target):
            self.status_message.emit(
                f"'{target.toString() or url}' is not a valid web address."
            )
            return False
        self._last_error = None
        self._view.setUrl(target)
        return True

    @staticmethod
    def _is_navigable(url: QUrl) -> bool:
        """Reject addresses Qt calls "valid" but cannot actually load.

        QUrl("http://") passes isValid() with an empty host, and setUrl() on it
        silently produces a blank page. Schemes that need an authority must
        actually have one.
        """
        if not url.isValid() or url.isEmpty():
            return False
        if url.scheme() in ("http", "https", "ftp", "ws", "wss") and not url.host():
            return False
        return True

    def back(self) -> None:
        self._view.back()

    def forward(self) -> None:
        self._view.forward()

    def reload(self) -> None:
        self._view.reload()

    def stop(self) -> None:
        self._view.stop()

    def can_go_back(self) -> bool:
        return self._view.history().canGoBack()

    def can_go_forward(self) -> bool:
        return self._view.history().canGoForward()

    def run_javascript(self, script: str, callback=None) -> None:
        """Evaluate JS in the page's own world.

        Used by the browser's own features and by tests. This is NOT part of
        the automation API: BrowserController never exposes it, so an
        automation caller cannot run arbitrary script against a page.
        """
        if callback is None:
            self._page.runJavaScript(script, MAIN_WORLD)
        else:
            self._page.runJavaScript(script, MAIN_WORLD, callback)

    def run_isolated_javascript(self, script: str, callback=None) -> None:
        """Run one of the browser's own scripts in the isolated world.

        The automation support script (page_script.js) lives in
        ApplicationWorld, where the page cannot see or tamper with it. Reaching
        it requires evaluating in that same world.
        """
        if callback is None:
            self._page.runJavaScript(script, ISOLATED_WORLD)
        else:
            self._page.runJavaScript(script, ISOLATED_WORLD, callback)

    def find_text(self, text: str, backward: bool = False, callback=None) -> None:
        """Find-in-page. ``callback`` receives (active_match, total_matches).

        Passing an empty string clears the highlight, which is what the find
        bar does when it closes.
        """
        flags = QWebEnginePage.FindFlag.FindBackward if backward else QWebEnginePage.FindFlag(0)
        if callback is None:
            self._page.findText(text, flags)
            return

        def on_result(result) -> None:
            callback(result.activeMatch(), result.numberOfMatches())

        self._page.findText(text, flags, on_result)

    def set_zoom(self, factor: float) -> None:
        self._view.setZoomFactor(max(0.25, min(5.0, factor)))

    def zoom(self) -> float:
        return self._view.zoomFactor()
