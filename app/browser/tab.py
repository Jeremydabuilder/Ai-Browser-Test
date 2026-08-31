"""A single browser tab: a web view plus the state the UI needs to show it."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.browser.profile import BrowserProfile
from app.browser.web_page import BrowserPage


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

    def __init__(
        self,
        profile: BrowserProfile,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._loading = False
        self._load_ok = True

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
        self._page.certificate_error_seen.connect(
            lambda desc: self.status_message.emit(f"Certificate error: {desc}")
        )
        self._page.fullScreenRequested.connect(self._on_fullscreen_requested)

    def _on_load_started(self) -> None:
        self._loading = True
        self.load_started.emit()

    def _on_load_finished(self, ok: bool) -> None:
        self._loading = False
        self._load_ok = ok
        if not ok:
            # Chromium already renders its own error page; we only surface a
            # short message so the user sees something in the status bar.
            self.status_message.emit(f"Could not load {self.url().toString()}")
        self.load_finished.emit(ok)

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

    def url(self) -> QUrl:
        return self._view.url()

    def title(self) -> str:
        return self._view.title() or self.url().host() or "New Tab"

    def icon(self) -> QIcon:
        return self._view.icon()

    # -- navigation API (also the surface a future AI agent will use) ----
    def navigate(self, url: QUrl | str) -> None:
        self._view.setUrl(QUrl(url) if isinstance(url, str) else url)

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
        """Evaluate JS in the page. The result is delivered asynchronously."""
        if callback is None:
            self._page.runJavaScript(script)
        else:
            self._page.runJavaScript(script, 0, callback)

    def find_text(self, text: str, backward: bool = False) -> None:
        flags = QWebEnginePage.FindFlag.FindBackward if backward else QWebEnginePage.FindFlag(0)
        self._view.findText(text, flags)

    def set_zoom(self, factor: float) -> None:
        self._view.setZoomFactor(max(0.25, min(5.0, factor)))

    def zoom(self) -> float:
        return self._view.zoomFactor()
