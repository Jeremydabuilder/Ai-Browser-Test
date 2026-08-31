"""A QWebEnginePage subclass with the behaviours a usable browser needs.

Subclassing the page (rather than only the view) is how you hook into the
engine: window.open, certificate errors, JS dialogs and console output all
arrive here.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEngineCertificateError, QWebEnginePage
from PySide6.QtWidgets import QMessageBox, QWidget


class BrowserPage(QWebEnginePage):
    """Page that delegates "open a new window" back to the tab manager."""

    # Emitted when the page wants a new tab/window. The listener must return a
    # page by calling ``provide_new_page`` - see ``create_window_requested``.
    link_hovered_changed = Signal(str)
    certificate_error_seen = Signal(str)

    def __init__(self, profile, parent: QObject | None = None) -> None:
        super().__init__(profile, parent)
        # A callable injected by the tab: given a window type, return a new
        # BrowserPage for the engine to load into. Keeping it a callback avoids
        # a circular import between page, tab and tab manager.
        self.new_page_factory = None
        self.linkHovered.connect(self.link_hovered_changed)
        # Qt 6 delivers certificate errors as a signal; older builds used a
        # virtual method. Connect defensively so we work on both.
        signal = getattr(self, "certificateError", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(self._on_certificate_error)

    # -- new windows ----------------------------------------------------
    def createWindow(self, window_type: QWebEnginePage.WebWindowType):  # noqa: N802
        """Called by Chromium for target=_blank links and window.open().

        Returning a page makes the engine load the request into it; returning
        None silently drops the navigation, which is why plain browsers that
        skip this look broken on sites full of _blank links.
        """
        if self.new_page_factory is None:
            return None
        return self.new_page_factory(window_type)

    # -- security -------------------------------------------------------
    def _on_certificate_error(self, error: QWebEngineCertificateError) -> None:
        """Reject bad certificates by default and tell the user why.

        We deliberately do NOT offer a one-click "proceed anyway": silently
        accepting broken TLS is the single easiest way to make a browser unsafe.
        """
        description = error.description()
        try:
            error.rejectCertificate()
        except AttributeError:  # pragma: no cover - very old Qt
            pass
        self.certificate_error_seen.emit(description)

    # -- JS dialogs -----------------------------------------------------
    def javaScriptAlert(self, origin: QUrl, msg: str) -> None:  # noqa: N802
        QMessageBox.information(self._dialog_parent(), origin.host() or "Page", msg)

    def javaScriptConfirm(self, origin: QUrl, msg: str) -> bool:  # noqa: N802
        answer = QMessageBox.question(
            self._dialog_parent(),
            origin.host() or "Page",
            msg,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Ok

    def javaScriptConsoleMessage(  # noqa: N802
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str,
        line: int,
        source: str,
    ) -> None:
        """Swallow page console noise.

        Every large site logs warnings; forwarding them to stderr would bury
        our own tracebacks. Override this while debugging a specific page.
        """
        return

    def _dialog_parent(self) -> QWidget | None:
        view = self.view()
        return view if isinstance(view, QWidget) else None
