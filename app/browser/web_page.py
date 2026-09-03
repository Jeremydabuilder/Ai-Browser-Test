"""A QWebEnginePage subclass with the behaviours a usable, safe browser needs.

Subclassing the page (rather than only the view) is how you hook into the
engine: window.open, certificate errors, permission prompts, HTTP auth, JS
dialogs and render-process crashes all arrive here.

Security posture, stated once so it is easy to audit:
  * Certificate errors are ALWAYS rejected. There is no click-through.
  * Permissions (camera, mic, location, notifications…) are denied unless the
    user explicitly allows them, per site, per session.
  * Protocol-handler registration is denied.
  * Nothing below is conditional on the hostname. This is a general-purpose
    browser; it has no per-site rules.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import (
    QWebEngineCertificateError,
    QWebEngineLoadingInfo,
    QWebEnginePage,
)
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QWidget

from app.browser.load_error import LoadError, from_loading_info


class BrowserPage(QWebEnginePage):
    """Page that reports load failures usefully and fails safe on security."""

    link_hovered_changed = Signal(str)
    load_error = Signal(object)             # LoadError
    certificate_rejected = Signal(str, str)  # host, human-readable reason
    render_process_crashed = Signal(str)
    permission_decided = Signal(str, bool)   # description, granted
    #: An action requested by one of PyBrowser's own internal pages.
    internal_action = Signal(str, dict)      # name, parameters

    def __init__(self, profile, parent: QObject | None = None) -> None:
        super().__init__(profile, parent)
        # Injected by BrowserTab: given a window type, return a page for the
        # engine to load into. A callback avoids a circular import between
        # page, tab and tab manager.
        self.new_page_factory = None
        self._last_error: LoadError | None = None
        #: Set when we refuse one of our own action URLs. Chromium reports a
        #: refused navigation as loadFinished(False), which is indistinguishable
        #: from a real failure unless we remember that we caused it.
        self._refused_action = False

        self.linkHovered.connect(self.link_hovered_changed)
        # loadingChanged carries QWebEngineLoadingInfo, which is the only place
        # Qt exposes the actual net error code. loadFinished(bool) does not.
        self.loadingChanged.connect(self._on_loading_changed)
        self.certificateError.connect(self._on_certificate_error)
        self.authenticationRequired.connect(self._on_authentication_required)
        self.proxyAuthenticationRequired.connect(self._on_proxy_authentication_required)
        self.renderProcessTerminated.connect(self._on_render_process_terminated)
        self.registerProtocolHandlerRequested.connect(self._on_protocol_handler_requested)
        self.permissionRequested.connect(self._on_permission_requested)

    # -- load results ---------------------------------------------------
    @property
    def last_error(self) -> LoadError | None:
        return self._last_error

    def take_refused_action(self) -> bool:
        """True once, if the load that just ended was an action we refused.

        Read-and-clear, because it answers a question about one specific
        loadFinished and must not colour the next one.
        """
        refused, self._refused_action = self._refused_action, False
        return refused

    def _on_loading_changed(self, info: QWebEngineLoadingInfo) -> None:
        status = info.status()
        if status == QWebEngineLoadingInfo.LoadStatus.LoadStartedStatus:
            self._last_error = None
            return
        if status == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus:
            error = from_loading_info(info)
            self._last_error = error
            if not error.is_silent:
                self.load_error.emit(error)
        elif status == QWebEngineLoadingInfo.LoadStatus.LoadSucceededStatus:
            self._last_error = None

    # -- internal pages -------------------------------------------------
    def acceptNavigationRequest(  # noqa: N802 - Qt's name
        self, url: QUrl, nav_type, is_main_frame: bool
    ) -> bool:
        """Intercept the new-tab page's action URLs; allow everything else.

        PyBrowser's internal pages cannot call Python, so they navigate to
        `pybrowser://newtab/action/...` to say what they want. We refuse the
        navigation and emit it instead, which keeps the decision in Python: the
        page states an intention and never carries it out itself.

        Only our own scheme is affected. A website cannot reach this - it would
        have to navigate the top frame to `pybrowser:`, which Chromium does not
        permit from a web origin - and even if one did, every action below is
        something the user could do from the menus anyway.
        """
        from app.browser.internal import parse_action

        action = parse_action(url)
        if action is not None and is_main_frame:
            name, params = action
            # Emit on the next turn of the event loop, not now. We are inside
            # Chromium's navigation-decision callback, and starting a fresh
            # navigation from in here re-enters the engine while it is still
            # deciding about this one - which aborts the render process
            # outright (SIGTRAP), not merely misbehaves. Deferring by one tick
            # lets the engine finish rejecting this navigation first.
            QTimer.singleShot(0, lambda: self.internal_action.emit(name, params))
            self._refused_action = True
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    # -- new windows ----------------------------------------------------
    def createWindow(self, window_type: QWebEnginePage.WebWindowType):  # noqa: N802
        """Called by Chromium for target=_blank links and window.open().

        Returning a page makes the engine load the request into it; returning
        None silently drops the navigation, which is why hand-rolled Qt
        browsers that skip this feel broken on sites full of _blank links.
        """
        if self.new_page_factory is None:
            return None
        return self.new_page_factory(window_type)

    # -- security -------------------------------------------------------
    def _on_certificate_error(self, error: QWebEngineCertificateError) -> None:
        """Reject bad certificates. Always.

        We deliberately do NOT offer a "proceed anyway" button. Accepting a
        broken certificate is the single easiest way to make a browser unsafe,
        and a browser that offers the bypass by default trains people to click
        it. Chromium still renders its own ERR_CERT_* page, and we add a plain
        explanation on top of it.
        """
        host = error.url().host()
        error.rejectCertificate()
        self.certificate_rejected.emit(host, error.description())

    def _on_permission_requested(self, permission) -> None:
        """Deny device/location/notification access unless the user allows it.

        Default-deny with an explicit prompt: the page cannot get the camera,
        microphone, screen, clipboard or location without a click. Qt's default
        without this handler is to leave the request hanging forever, which
        looks like a broken page.
        """
        description = self._describe_permission(permission)
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Permission request",
            f"{permission.origin().host() or 'This page'} wants to {description}.\n\nAllow?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,   # default is deny
        )
        granted = answer == QMessageBox.StandardButton.Yes
        permission.grant() if granted else permission.deny()
        self.permission_decided.emit(description, granted)

    @staticmethod
    def _describe_permission(permission) -> str:
        try:
            name = permission.permissionType().name
        except AttributeError:  # pragma: no cover - defensive
            return "use a device feature"
        return {
            "MediaAudioCapture": "use your microphone",
            "MediaVideoCapture": "use your camera",
            "MediaAudioVideoCapture": "use your camera and microphone",
            "DesktopVideoCapture": "record your screen",
            "DesktopAudioVideoCapture": "record your screen and audio",
            "Geolocation": "know your location",
            "Notifications": "show notifications",
            "ClipboardReadWrite": "read your clipboard",
            "LocalFontsAccess": "see your installed fonts",
        }.get(name, f"use {name}")

    def _on_protocol_handler_requested(self, request) -> None:
        """Refuse to let pages register themselves as protocol handlers.

        Allowing this lets a site claim mailto:, webcal: and friends. It is a
        real capability, but it needs a considered UI; silently granting it is
        worse than not supporting it.
        """
        request.reject()

    def _on_render_process_terminated(self, status, exit_code: int) -> None:
        """The tab's renderer died; tell the user instead of showing a blank page."""
        crashed = status != QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus
        if crashed:
            self.render_process_crashed.emit(
                "This page stopped responding and was closed. Reload to try again."
            )

    # -- credentials ----------------------------------------------------
    def _on_authentication_required(self, url: QUrl, authenticator) -> None:
        """Handle HTTP basic/digest auth prompts."""
        self._prompt_for_credentials(
            authenticator, f"{url.host()} requires a username and password."
        )

    def _on_proxy_authentication_required(self, _url, authenticator, proxy_host: str) -> None:
        self._prompt_for_credentials(
            authenticator, f"The proxy {proxy_host} requires a username and password."
        )

    def _prompt_for_credentials(self, authenticator, message: str) -> None:
        parent = self._dialog_parent()
        user, ok = QInputDialog.getText(parent, "Sign in", f"{message}\n\nUsername:")
        if not ok or not user:
            # Leaving the authenticator untouched makes Qt cancel the request,
            # which surfaces as a normal 401 page rather than a hang.
            return
        password, ok = QInputDialog.getText(
            parent, "Sign in", "Password:", QLineEdit.EchoMode.Password
        )
        if not ok:
            return
        authenticator.setUser(user)
        authenticator.setPassword(password)

    # -- JS dialogs -----------------------------------------------------
    def javaScriptAlert(self, origin: QUrl, msg: str) -> None:  # noqa: N802
        QMessageBox.information(self._dialog_parent(), self._origin_label(origin), msg)

    def javaScriptConfirm(self, origin: QUrl, msg: str) -> bool:  # noqa: N802
        answer = QMessageBox.question(
            self._dialog_parent(),
            self._origin_label(origin),
            msg,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Ok

    def javaScriptPrompt(self, origin: QUrl, msg: str, default: str):  # noqa: N802
        """Implement window.prompt().

        Without this override Qt returns False and the page's prompt() gets
        null, which quietly breaks older sites and some login flows.
        """
        text, ok = QInputDialog.getText(
            self._dialog_parent(),
            self._origin_label(origin),
            msg,
            QLineEdit.EchoMode.Normal,
            default,
        )
        return ok, text

    def javaScriptConsoleMessage(  # noqa: N802
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str,
        line: int,
        source: str,
    ) -> None:
        """Swallow page console noise.

        Every large site logs warnings; forwarding them to stderr would bury the
        application's own output. Set PYBROWSER_JS_CONSOLE=1 to see them.
        """
        import os

        if os.environ.get("PYBROWSER_JS_CONSOLE") == "1":
            print(f"[js] {source}:{line} {message}", flush=True)

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _origin_label(origin: QUrl) -> str:
        return origin.host() or "This page"

    def _dialog_parent(self) -> QWidget | None:
        view = self.view()
        return view if isinstance(view, QWidget) else None
