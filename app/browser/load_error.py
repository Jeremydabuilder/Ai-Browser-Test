"""Turning Chromium network error codes into something a person can read.

Qt reports load failures as an integer net error code plus an internal string
like "ERR_NAME_NOT_RESOLVED". Those are fine in a log and useless in a status
bar, so this module maps them to a plain-English sentence and a category the UI
can act on. Nothing here is site-specific: it is a pure function of the error
code, exactly like Chrome's own error pages.

Code ranges follow Chromium's net/base/net_error_list.h:
    -1..-99    system / connection
    -100..-199 certificate and TLS
    -200..-299 certificate validation
    -300..-399 HTTP
    -400..-499 cache
    -800..-899 DNS
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWebEngineCore import QWebEngineLoadingInfo


class ErrorCategory:
    """Coarse grouping used to decide what the UI should say and offer."""

    NONE = "none"
    NETWORK = "network"        # unreachable, refused, timed out
    DNS = "dns"                # host does not resolve
    CERTIFICATE = "certificate"  # TLS/certificate problem
    HTTP = "http"              # protocol-level failure
    BLOCKED = "blocked"        # blocked by a proxy, policy or the client
    CONTENT = "content"        # malformed response, unsupported scheme
    ABORTED = "aborted"        # user navigated away; not an error worth showing
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoadError:
    """A page load failure, described for humans and for code."""

    url: str
    code: int
    category: str
    message: str          # shown to the user; no stack traces, no jargon
    technical: str        # e.g. "ERR_NAME_NOT_RESOLVED (-105)" - for the tooltip

    @property
    def is_silent(self) -> bool:
        """True for failures that should not be reported (user aborted a load)."""
        return self.category == ErrorCategory.ABORTED


# Specific codes worth a tailored sentence. Anything not listed falls back to
# the range-based defaults below, so an unrecognised code still reads sensibly.
_SPECIFIC: dict[int, tuple[str, str]] = {
    -2:   (ErrorCategory.CONTENT, "The download or navigation failed."),
    -3:   (ErrorCategory.ABORTED, "Navigation was cancelled."),
    -6:   (ErrorCategory.CONTENT, "The file could not be found."),
    -7:   (ErrorCategory.NETWORK, "The site took too long to respond."),
    -10:  (ErrorCategory.CONTENT, "This address uses a protocol the browser cannot open."),
    -20:  (ErrorCategory.BLOCKED, "The request was blocked by the browser."),
    -21:  (ErrorCategory.NETWORK, "The network connection changed during loading."),
    -22:  (ErrorCategory.BLOCKED, "The request was blocked by an administrator policy."),
    -100: (ErrorCategory.NETWORK, "The connection was closed unexpectedly."),
    -101: (ErrorCategory.NETWORK, "The connection was reset."),
    -102: (ErrorCategory.NETWORK, "The site refused the connection."),
    -104: (ErrorCategory.NETWORK, "Could not connect to the site."),
    -105: (ErrorCategory.DNS, "That address could not be found. Check the spelling of the site name."),
    -106: (ErrorCategory.NETWORK, "The internet connection appears to be offline."),
    -107: (ErrorCategory.CERTIFICATE, "The secure connection failed."),
    -108: (ErrorCategory.NETWORK, "The site's address is unreachable."),
    -109: (ErrorCategory.NETWORK, "The site is unreachable."),
    -113: (ErrorCategory.CERTIFICATE, "The site did not complete a secure connection."),
    -118: (ErrorCategory.NETWORK, "The connection timed out."),
    -130: (ErrorCategory.BLOCKED, "The proxy server refused the connection."),
    -137: (ErrorCategory.DNS, "The proxy server's address could not be resolved."),
    -138: (ErrorCategory.BLOCKED, "The proxy server refused this request."),
    -200: (ErrorCategory.CERTIFICATE, "The site's security certificate is for a different address."),
    -201: (ErrorCategory.CERTIFICATE, "The site's security certificate has expired or is not yet valid."),
    -202: (ErrorCategory.CERTIFICATE, "The site's security certificate is not trusted."),
    -207: (ErrorCategory.CERTIFICATE, "The site's security certificate could not be checked."),
    -310: (ErrorCategory.HTTP, "The site redirected too many times."),
    -312: (ErrorCategory.BLOCKED, "The browser blocks this network port for security reasons."),
    -324: (ErrorCategory.HTTP, "The site sent no data."),
    -348: (ErrorCategory.BLOCKED, "A proxy or filter blocked this page."),
    -501: (ErrorCategory.CERTIFICATE, "This page was not delivered securely."),
}

_RANGES: list[tuple[int, int, str, str]] = [
    (-99, -1, ErrorCategory.NETWORK, "The connection failed."),
    (-199, -100, ErrorCategory.CERTIFICATE, "The secure connection failed."),
    (-299, -200, ErrorCategory.CERTIFICATE, "There is a problem with the site's security certificate."),
    (-399, -300, ErrorCategory.HTTP, "The site sent a response the browser could not use."),
    (-499, -400, ErrorCategory.CONTENT, "The page could not be read from the cache."),
    (-599, -500, ErrorCategory.UNKNOWN, "The page could not be loaded."),
    (-699, -600, ErrorCategory.CONTENT, "The page could not be loaded."),
    (-799, -700, ErrorCategory.CONTENT, "The page could not be loaded."),
    (-899, -800, ErrorCategory.DNS, "That address could not be found."),
]


def describe(url: str, code: int, error_string: str = "") -> LoadError:
    """Build a LoadError for a Chromium net error code.

    ``error_string`` is Qt's internal name (ERR_...); it goes in the tooltip,
    never in the main message.
    """
    if code == 0:
        return LoadError(url, 0, ErrorCategory.NONE, "", "")

    category, message = ErrorCategory.UNKNOWN, "The page could not be loaded."
    if code in _SPECIFIC:
        category, message = _SPECIFIC[code]
    else:
        for low, high, cat, msg in _RANGES:
            if low <= code <= high:
                category, message = cat, msg
                break

    technical = f"{error_string} ({code})" if error_string else str(code)
    return LoadError(url, code, category, message, technical)


# Qt groups failures into domains. When a specific code is not in our table the
# domain is still authoritative about what kind of failure it was, so we trust
# it over the numeric range guess.
_DOMAIN_CATEGORY = {
    "ConnectionErrorDomain": ErrorCategory.NETWORK,
    "CertificateErrorDomain": ErrorCategory.CERTIFICATE,
    "DnsErrorDomain": ErrorCategory.DNS,
    "HttpErrorDomain": ErrorCategory.HTTP,
    "FtpErrorDomain": ErrorCategory.NETWORK,
    "InternalErrorDomain": ErrorCategory.UNKNOWN,
}


def from_loading_info(info: QWebEngineLoadingInfo) -> LoadError:
    """Build a LoadError from Qt's QWebEngineLoadingInfo."""
    code = info.errorCode()
    error_string = info.errorString() or ""
    domain = info.errorDomain()

    # HttpStatusCodeDomain means we reached the server and it answered with an
    # error status - a very different story from "could not connect".
    if domain == QWebEngineLoadingInfo.ErrorDomain.HttpStatusCodeDomain:
        return LoadError(
            info.url().toString(),
            code,
            ErrorCategory.HTTP,
            f"The site returned an error (HTTP {code}).",
            error_string or f"HTTP {code}",
        )

    error = describe(info.url().toString(), code, error_string)
    # Only override when we fell back to a range guess; an explicitly mapped
    # code (e.g. ERR_UNSAFE_PORT living in the HTTP domain) knows better.
    if code not in _SPECIFIC:
        domain_name = getattr(domain, "name", "")
        category = _DOMAIN_CATEGORY.get(domain_name)
        if category is not None and category != error.category:
            error = LoadError(error.url, error.code, category, error.message, error.technical)
    return error
