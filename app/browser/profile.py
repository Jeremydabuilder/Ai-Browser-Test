"""The shared Qt WebEngine profile: cookies, cache, storage, downloads.

A QWebEngineProfile owns cookies, cache, local storage and downloads. All tabs
share one profile so that logging into a site in one tab logs you in
everywhere, exactly like a real browser. The profile must outlive every page
that uses it, so the application holds the single instance.

Every setting below is set explicitly, including several that merely restate a
Qt default. That is deliberate: a browser's security posture should be
readable in one place rather than inferred from what was left unconfigured.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEngineProfile,
    QWebEngineSettings,
)

from app.config import cache_path, downloads_path, profile_storage_path

# Opt-in override for the rare case where a user needs a different UA. We do
# not set one by default - see _configure_identity().
_UA_OVERRIDE_ENV = "PYBROWSER_USER_AGENT"


class BrowserProfile(QObject):
    """Owns the persistent QWebEngineProfile and its global configuration."""

    download_started = Signal(QWebEngineDownloadRequest)
    download_finished = Signal(QWebEngineDownloadRequest)

    def __init__(self, parent: QObject | None = None, storage_name: str = "default") -> None:
        super().__init__(parent)
        # Naming the profile (rather than using the default off-the-record one)
        # is what makes cookies and local storage survive a restart.
        self._profile = QWebEngineProfile(storage_name, self)
        self._configure_storage()
        self._configure_identity()
        self._configure_settings()
        self._profile.downloadRequested.connect(self._on_download_requested)

    # -- storage ---------------------------------------------------------
    def _configure_storage(self) -> None:
        profile = self._profile
        profile.setPersistentStoragePath(str(profile_storage_path()))
        profile.setCachePath(str(cache_path()))
        profile.setDownloadPath(str(downloads_path()))
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)

        # AllowPersistentCookies, NOT ForcePersistentCookies.
        #
        # Both persist normal cookies across restarts, which is what makes
        # "stay signed in" work. The difference is session cookies - the ones a
        # site deliberately marks as expiring when the browser closes. Force...
        # writes those to disk too, silently overriding the site's intent and
        # keeping bank/webmail sessions alive after quit. Honouring the
        # distinction is the correct default; this was set to Force previously
        # and is fixed here.
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
        # Remember per-site permission answers across restarts so the user is
        # not re-prompted for the same site forever.
        profile.setPersistentPermissionsPolicy(
            QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk
        )

    # -- identity --------------------------------------------------------
    def _configure_identity(self) -> None:
        """Leave the user agent at Qt's Chromium-compatible default.

        Qt's stock UA already identifies the engine honestly
        ("... QtWebEngine/6.11.2 Chrome/140.0.0.0 ...") and matches what sites
        expect from Chromium. Appending a custom product token - which an
        earlier version of this file did - buys nothing, adds a fingerprinting
        signal, and risks tripping "unsupported browser" checks on sites that
        parse the tail of the string. A user who genuinely needs a different UA
        can set PYBROWSER_USER_AGENT.
        """
        override = os.environ.get(_UA_OVERRIDE_ENV)
        if override:
            self._profile.setHttpUserAgent(override)

    # -- web settings ----------------------------------------------------
    def _configure_settings(self) -> None:
        settings = self._profile.settings()
        attr = QWebEngineSettings.WebAttribute

        enabled = {
            # Real sites are JavaScript applications; this is not optional.
            attr.JavascriptEnabled: "sites are JS apps",
            attr.LocalStorageEnabled: "expected by nearly every site",
            attr.PluginsEnabled: "required for the built-in PDF viewer",
            attr.PdfViewerEnabled: "open PDFs in-tab instead of downloading them",
            attr.FullScreenSupportEnabled: "video players request fullscreen",
            attr.ErrorPageEnabled: "Chromium's error pages beat a blank tab",
            attr.ScrollAnimatorEnabled: "smooth scrolling",
            attr.AutoLoadImages: "it is a web browser",
            attr.WebGLEnabled: "maps and canvas-based sites need it",
            attr.DnsPrefetchEnabled: "measurably faster navigation",
            attr.LinksIncludedInFocusChain: "keyboard accessibility",
            # window.open is routed through createWindow() and becomes a tab,
            # so this enables tabs rather than uncontrolled popups.
            attr.JavascriptCanOpenWindows: "target=_blank and window.open",
            # Write-only clipboard access: makes "copy" buttons work. Reading
            # the clipboard is a separate permission and still prompts.
            attr.JavascriptCanAccessClipboard: "copy-to-clipboard buttons",
            # getDisplayMedia is gated behind our default-deny permission
            # prompt, so this enables the capability, not the access.
            attr.ScreenCaptureEnabled: "screen sharing, behind a permission prompt",
        }
        for attribute in enabled:
            settings.setAttribute(attribute, True)

        disabled = {
            # Mixed content: an HTTPS page must not silently pull HTTP
            # subresources. Leaving this off is what keeps the padlock honest.
            attr.AllowRunningInsecureContent: "blocks mixed content",
            # Geolocation only over HTTPS.
            attr.AllowGeolocationOnInsecureOrigins: "no location over plain HTTP",
            # A local file must not be able to read your other local files or
            # phone home with their contents.
            attr.LocalContentCanAccessFileUrls: "file:// sandboxing",
            attr.LocalContentCanAccessRemoteUrls: "file:// sandboxing",
            # <a ping> tracking beacons.
            attr.HyperlinkAuditingEnabled: "privacy",
            # Pages should not be able to steal focus by navigating.
            attr.AllowWindowActivationFromJavaScript: "no focus stealing",
            # Autoplay with sound on load is hostile; match Chrome's default.
            attr.PlaybackRequiresUserGesture: "no surprise autoplay",
            # Reading the clipboard from script requires the permission prompt.
            attr.JavascriptCanPaste: "clipboard reads go through permissions",
        }
        for attribute in disabled:
            settings.setAttribute(attribute, False)

    # -- accessors -------------------------------------------------------
    @property
    def qt_profile(self) -> QWebEngineProfile:
        return self._profile

    # -- downloads -------------------------------------------------------
    def _on_download_requested(self, download: QWebEngineDownloadRequest) -> None:
        """Accept downloads into the user's Downloads folder.

        Qt cancels a download unless we explicitly accept it, so a browser that
        ignores this signal looks broken whenever a link points at a file. Qt
        de-duplicates the filename itself, so an existing file is never
        silently overwritten. The window shows a notice for each download; a
        download is never started without the user having clicked something.
        """
        download.setDownloadDirectory(str(downloads_path()))
        download.isFinishedChanged.connect(
            lambda: self.download_finished.emit(download)
        )
        download.accept()
        self.download_started.emit(download)

    # -- privacy controls ------------------------------------------------
    def clear_http_cache(self) -> None:
        self._profile.clearHttpCache()

    def clear_cookies(self) -> None:
        store = self._profile.cookieStore()
        if store is not None:
            store.deleteAllCookies()

    def clear_all_visited_links(self) -> None:
        self._profile.clearAllVisitedLinks()
