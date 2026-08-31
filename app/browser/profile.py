"""The shared Qt WebEngine profile.

A QWebEngineProfile owns cookies, cache, local storage and downloads. All tabs
share one profile so that logging into a site in one tab logs you in
everywhere, exactly like a real browser. The profile must outlive every page
that uses it, so the application holds the single instance.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEngineProfile,
    QWebEngineSettings,
)

from app import APP_NAME, __version__
from app.config import cache_path, downloads_path, profile_storage_path

# A plain Chrome-ish UA avoids sites serving us a degraded "unsupported
# browser" page. Qt already appends QtWebEngine/Chrome tokens; we only pin the
# product name so the string stays honest.
_PRODUCT = f"{APP_NAME}/{__version__}"


class BrowserProfile(QObject):
    """Owns the persistent QWebEngineProfile and its global configuration."""

    download_started = Signal(QWebEngineDownloadRequest)
    download_finished = Signal(QWebEngineDownloadRequest)

    def __init__(self, parent: QObject | None = None, storage_name: str = "default") -> None:
        super().__init__(parent)
        # Naming the profile (rather than using the default off-the-record one)
        # is what makes cookies and local storage survive a restart.
        self._profile = QWebEngineProfile(storage_name, self)
        self._profile.setPersistentStoragePath(str(profile_storage_path()))
        self._profile.setCachePath(str(cache_path()))
        self._profile.setDownloadPath(str(downloads_path()))
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        self._profile.setHttpUserAgent(
            f"{self._profile.httpUserAgent()} {_PRODUCT}"
        )
        self._configure_settings()
        self._profile.downloadRequested.connect(self._on_download_requested)

    def _configure_settings(self) -> None:
        settings = self._profile.settings()
        attr = QWebEngineSettings.WebAttribute
        for attribute in (
            attr.JavascriptEnabled,            # real sites are JS apps
            attr.LocalStorageEnabled,
            attr.PluginsEnabled,               # required for the built-in PDF viewer
            attr.PdfViewerEnabled,
            attr.FullScreenSupportEnabled,     # YouTube fullscreen
            attr.ScreenCaptureEnabled,
            attr.JavascriptCanOpenWindows,
            attr.JavascriptCanAccessClipboard,
            attr.ErrorPageEnabled,
            attr.ScrollAnimatorEnabled,
            attr.AutoLoadImages,
            attr.WebGLEnabled,
            attr.DnsPrefetchEnabled,
        ):
            settings.setAttribute(attribute, True)
        # Autoplay with sound on page load is hostile; keep Chrome's default.
        settings.setAttribute(attr.PlaybackRequiresUserGesture, True)

    @property
    def qt_profile(self) -> QWebEngineProfile:
        return self._profile

    def _on_download_requested(self, download: QWebEngineDownloadRequest) -> None:
        """Accept downloads into the user's Downloads folder.

        Qt cancels a download unless we explicitly accept it, so a browser that
        ignores this signal looks broken whenever a link points at a file.
        """
        download.setDownloadDirectory(str(downloads_path()))
        download.accept()
        download.isFinishedChanged.connect(
            lambda: self.download_finished.emit(download)
        )
        self.download_started.emit(download)

    def clear_http_cache(self) -> None:
        self._profile.clearHttpCache()

    def clear_cookies(self) -> None:
        store = self._profile.cookieStore()
        if store is not None:
            store.deleteAllCookies()
