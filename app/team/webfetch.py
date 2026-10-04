"""Read a public web page for the Researcher - safely.

The addresses come from a search engine, so they are attacker-influenced. The
fetcher therefore treats every URL as hostile:

* **Only public internet addresses.** The host is resolved first and *every*
  resulting address must be globally routable. Loopback, private (RFC 1918 /
  ULA), link-local (incl. the 169.254.169.254 cloud-metadata address), CGNAT
  (100.64/10), multicast, reserved and unspecified addresses are refused, as are
  IPv4 addresses hidden inside IPv6 forms (``::ffff:127.0.0.1``, 6to4, NAT64,
  Teredo) and obfuscated numeric hosts (``2130706433``, ``0x7f.1``).
* **No DNS-rebinding window.** The connection is made to the address that was
  validated (the URL carries the IP, the ``Host`` header and TLS server name
  carry the hostname), so a second, different DNS answer is never used.
* **Redirects are re-checked.** Each hop is validated like a fresh URL; at most
  ``max_redirects`` are followed; a redirect to a private address is refused.
* **Narrow scheme/port/credentials.** ``http``/``https`` on ports 80/443 only,
  no ``user:pass@``, single-label and ``.local``/``.internal``/``.localhost``
  names refused.
* **Bounded.** Per-request timeouts plus an overall deadline, a decoded-byte
  cap (so a gzip bomb stops at the cap), a character cap on the extracted text,
  and a content-type allow-list (HTML and plain text only).
* **Anonymous.** No cookies, no credentials, no referrer; a descriptive
  User-Agent.

The text extractor drops scripts, styles, navigation, forms and visually hidden
elements (a common hiding place for prompt injection) and prefers
``<article>``/``<main>``. What it returns is still *untrusted data*; callers
fence it like any page.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx2 as httpx

USER_AGENT = "PyBrowser-Team/1.0 (+reads one public page for a research task you started)"
ALLOWED_PORTS = (80, 443)
MAX_URL = 2000
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".private")
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="team-dns")


class FetchErrorKind:
    BAD_URL = "bad_url"
    BLOCKED = "blocked_address"
    DNS = "dns"
    TIMEOUT = "timeout"
    NETWORK = "network"
    HTTP = "http"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    UNSUPPORTED = "unsupported"
    EMPTY = "empty"


class FetchError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Page:
    url: str                  # the final address that answered
    title: str
    text: str
    truncated: bool = False
    content_type: str = ""
    bytes_read: int = 0
    redirects: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Address policy
# ---------------------------------------------------------------------------


def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour:
        found.append(ip.sixtofour)
    if ip.teredo:
        found += [ip.teredo[0], ip.teredo[1]]
    packed = ip.packed
    if ip in ipaddress.ip_network("64:ff9b::/96"):          # NAT64
        found.append(ipaddress.IPv4Address(packed[-4:]))
    if ip in ipaddress.ip_network("::/96"):                  # IPv4-compatible (deprecated)
        found.append(ipaddress.IPv4Address(packed[-4:]))
    return found


def blocked_reason(address: str) -> str:
    """Empty string if ``address`` is a public internet address, else why not."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return "not a valid IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        for inner in _embedded_v4(ip):
            reason = blocked_reason(str(inner))
            if reason:
                return f"{reason} (embedded in an IPv6 address)"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address (cloud metadata lives here)"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_private:
        return "a private-network address"
    if not ip.is_global:
        return "not a public internet address"
    return ""


_NUMERIC_HOST = re.compile(r"^[0-9a-fx.]+$", re.IGNORECASE)


def validate_url(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, target) for a URL the fetcher may even *consider*.
    Raises FetchError(BAD_URL / BLOCKED)."""
    if not url or len(url) > MAX_URL or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise FetchError(FetchErrorKind.BAD_URL, "The address is empty, too long or contains whitespace.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError(FetchErrorKind.BAD_URL, "The address is malformed.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise FetchError(FetchErrorKind.BAD_URL, "Only http and https pages can be read.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError(FetchErrorKind.BAD_URL, "Addresses with embedded credentials are refused.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError(FetchErrorKind.BAD_URL, "The address has no host.")
    if not host.isascii():
        try:                                   # one canonical ASCII form for DNS, Host header and TLS name
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise FetchError(FetchErrorKind.BAD_URL, "The host name is not valid.") from None
    if "%" in host or not re.fullmatch(r"[a-z0-9.:\-_\[\]]+", host):
        raise FetchError(FetchErrorKind.BAD_URL, "The host name has characters that are not allowed.")
    scheme = parts.scheme.lower()
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise FetchError(FetchErrorKind.BLOCKED, f"Port {port} is not allowed (only 80 and 443).")
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        reason = blocked_reason(host)
        if reason:
            raise FetchError(FetchErrorKind.BLOCKED, f"{host} is {reason}.")
    else:
        if _NUMERIC_HOST.match(host):
            raise FetchError(FetchErrorKind.BLOCKED, "Numeric host forms are refused (use a normal hostname).")
        if "." not in host or host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
            raise FetchError(FetchErrorKind.BLOCKED, f"{host} looks like a local or internal name.")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return scheme, host, port, target


Resolver = Callable[[str, int], "list[str]"]


def system_resolver(host: str, port: int, timeout: float = 6.0) -> list[str]:
    def lookup() -> list[str]:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})

    try:
        return _DNS_POOL.submit(lookup).result(timeout)
    except FutureTimeout:
        raise FetchError(FetchErrorKind.DNS, f"Looking up {host} took too long.") from None
    except OSError:
        raise FetchError(FetchErrorKind.DNS, f"Could not find {host}.") from None


def public_addresses(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve ``host`` and require EVERY answer to be public."""
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = list(resolver(host, port))
    if not addresses:
        raise FetchError(FetchErrorKind.DNS, f"Could not find {host}.")
    for address in addresses:
        reason = blocked_reason(address)
        if reason:
            raise FetchError(FetchErrorKind.BLOCKED,
                             f"{host} resolves to {address}, which is {reason}. Only public websites are read.")
    return addresses


# ---------------------------------------------------------------------------
# HTML -> readable text
# ---------------------------------------------------------------------------

_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "canvas", "object", "embed",
         "select", "option", "button", "input", "textarea", "head"}
_BOILERPLATE = {"nav", "aside", "footer", "header", "form", "menu", "dialog"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "li", "ul", "ol", "table", "tr", "br",
          "blockquote", "pre", "figure", "figcaption", "dd", "dt", "dl", "h1", "h2", "h3", "h4", "h5", "h6",
          "hr", "details", "summary"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "area", "base", "col", "embed", "param", "track", "wbr"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|%)?\s*(?:;|$)"
                           r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|height\s*:\s*0(?:px)?\s*;[^\"]*overflow\s*:\s*hidden",
                           re.IGNORECASE)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.charset = ""
        self._stack: list[tuple[str, bool, bool, bool]] = []     # (tag, skip, boilerplate, main)
        self._in_title = False
        self.blocks: list[tuple[str, bool, bool]] = []            # (text, boilerplate, in_main)
        self._buf: list[str] = []
        self._prefix = ""

    def _state(self) -> tuple[bool, bool, bool]:
        skip = any(s for _t, s, _b, _m in self._stack)
        main = any(m for _t, _s, _b, m in self._stack)
        # A <header>/<footer> inside an <article>/<main> belongs to that content
        # (title, byline); at page level it is site chrome.
        boiler = any(b and not (main and t in ("header", "footer")) for t, _s, b, _m in self._stack)
        return skip, boiler, main

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        prefix, self._prefix = self._prefix, ""
        if len(text) >= 2:
            _skip, boiler, main = self._state()
            self.blocks.append((prefix + text, boiler, main))

    def handle_starttag(self, tag, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            if a.get("charset"):
                self.charset = a["charset"]
            content_type = a.get("content", "")
            if a.get("http-equiv", "").lower() == "content-type" and "charset=" in content_type.lower():
                self.charset = content_type.lower().split("charset=")[-1].strip(" ;\"'")
            if a.get("property", "").lower() == "og:title":
                self.og_title = a.get("content", "")
            return
        if tag in _VOID:
            if tag in _BLOCK:
                self._flush()
            return
        hidden = ("hidden" in a or a.get("aria-hidden", "").lower() == "true"
                  or bool(_HIDDEN_STYLE.search(a.get("style", ""))))
        skip = tag in _SKIP or hidden
        boiler = tag in _BOILERPLATE or a.get("role", "").lower() in ("navigation", "banner", "complementary",
                                                                      "contentinfo")
        main = tag in ("article", "main") or a.get("role", "").lower() == "main"
        if tag in _BLOCK or skip or boiler or main:
            self._flush()            # text before this element must not take its flags
        if tag == "title":
            self._in_title = True
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._prefix = "#" * int(tag[1]) + " "
        elif tag == "li":
            self._prefix = "- "
        elif tag in ("td", "th") and self._buf:
            self._buf.append(" | ")
        self._stack.append((tag, skip, boiler, main))

    def handle_endtag(self, tag) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._flush()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                if any(flag for flag in self._stack[index][1:]):
                    self._flush()    # text inside a flagged element is judged with its flags
                del self._stack[index:]
                break

    def handle_data(self, data) -> None:
        if self._in_title:
            self.title += data
            return
        if self._state()[0]:
            return
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def extract_text(html: str, max_chars: int) -> tuple[str, str, bool]:
    """(title, text, truncated) from an HTML document."""
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must never break a mission
        pass
    mains = [t for t, boiler, main in parser.blocks if main and not boiler]
    chosen = mains if sum(len(t) for t in mains) >= 400 else [t for t, boiler, _m in parser.blocks if not boiler]
    lines: list[str] = []
    for line in chosen:
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    text = "\n".join(lines)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rsplit("\n", 1)[0] if "\n" in text[:max_chars] else text[:max_chars]
        text += "\n[page truncated]"
    title = " ".join((parser.og_title or parser.title).split())[:200]
    return title, text, truncated


# ---------------------------------------------------------------------------
# The fetcher
# ---------------------------------------------------------------------------


class _BoundedDecoder:
    """Content-Encoding decoder that never produces more than it is allowed to."""

    def __init__(self, encoding: str) -> None:
        name = encoding.strip().lower()
        self._name = name
        self._tried_raw = False
        if name in ("", "identity"):
            self._d = None
        elif name in ("gzip", "x-gzip"):
            self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif name == "deflate":
            self._d = zlib.decompressobj()
        else:
            raise FetchError(FetchErrorKind.UNSUPPORTED, f"The site used an unsupported encoding ({name[:20]}).")

    def feed(self, data: bytes, room: int) -> tuple[bytes, bool]:
        """(decoded bytes, True when ``room`` ran out)."""
        if room <= 0:
            return b"", True
        if self._d is None:
            return (data[:room], len(data) > room)
        out: list[bytes] = []
        pending = data
        try:
            while pending and room > 0:
                piece = self._d.decompress(pending, room)
                out.append(piece)
                room -= len(piece)
                pending = self._d.unconsumed_tail
                if not piece and not pending:
                    break
        except zlib.error:
            if self._name == "deflate" and not self._tried_raw and not out:
                self._tried_raw = True                      # some servers send raw deflate
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)
                return self.feed(data, room)
            raise FetchError(FetchErrorKind.NETWORK, "The site sent data that could not be decoded.") from None
        return b"".join(out), room <= 0 and bool(pending)


class PageFetcher:
    def __init__(self, *, timeout: float = 10.0, deadline: float = 20.0, max_bytes: int = 1_500_000,
                 max_chars: int = 20_000, max_redirects: int = 4, resolver: Resolver | None = None,
                 transport: "httpx.BaseTransport | None" = None, clock=time.monotonic) -> None:
        self.timeout = timeout
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.max_redirects = max_redirects
        self._resolver = resolver or system_resolver
        self._transport = transport
        self._clock = clock

    def fetch(self, url: str) -> Page:
        started = self._clock()
        current = url.split("#", 1)[0]
        redirects: list[str] = []
        for _hop in range(self.max_redirects + 1):
            scheme, host, port, target = validate_url(current)
            addresses = public_addresses(host, port, self._resolver)
            response_info = self._request(scheme, host, port, target, addresses[0], started)
            if isinstance(response_info, str):                    # a redirect target
                redirects.append(response_info)
                current = urljoin(current, response_info).split("#", 1)[0]
                continue
            body, content_type, charset, clipped = response_info
            return self._page(current, body, content_type, charset, clipped, redirects)
        raise FetchError(FetchErrorKind.TOO_MANY_REDIRECTS, f"More than {self.max_redirects} redirects.")

    def _request(self, scheme: str, host: str, port: int, target: str, address: str, started: float):
        ip_host = f"[{address}]" if ":" in address else address
        default_port = 443 if scheme == "https" else 80
        netloc = ip_host if port == default_port else f"{ip_host}:{port}"
        host_header = host if port == default_port else f"{host}:{port}"
        pinned = f"{scheme}://{netloc}{target}"
        headers = {"Host": host_header, "User-Agent": USER_AGENT, "Accept-Language": "en;q=0.9, *;q=0.5",
                   "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                   "Accept-Encoding": "gzip, deflate", "DNT": "1"}
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        timeout = httpx.Timeout(connect=min(5.0, self.timeout), read=self.timeout, write=5.0, pool=5.0)
        try:
            with httpx.Client(timeout=timeout, transport=self._transport, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("GET", pinned, headers=headers, extensions=extensions) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        if not location:
                            raise FetchError(FetchErrorKind.HTTP, f"Redirect ({status}) without a destination.")
                        return location
                    if status >= 400:
                        raise FetchError(FetchErrorKind.HTTP, f"The site answered {status}.")
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type and content_type not in _TEXT_TYPES:
                        raise FetchError(FetchErrorKind.UNSUPPORTED,
                                         f"Not a text page ({content_type}); only HTML and plain text are read.")
                    charset = ""
                    header_type = response.headers.get("content-type", "").lower()
                    if "charset=" in header_type:
                        charset = header_type.split("charset=")[-1].split(";")[0].strip(" \"'")
                    decoder = _BoundedDecoder(response.headers.get("content-encoding", ""))
                    chunks: list[bytes] = []
                    total = raw_total = 0
                    clipped = False
                    # Raw (still compressed) bytes are read and inflated by us, never by the HTTP
                    # client, so memory stays bounded by max_bytes even for a gzip/deflate bomb.
                    # (A test double's in-memory body is already consumed; a real response never is.)
                    stream = list(response.stream) if response.is_stream_consumed else response.iter_raw()
                    for raw in stream:
                        if self._clock() - started > self.deadline:
                            raise FetchError(FetchErrorKind.TIMEOUT, "Reading the page took too long.")
                        raw_total += len(raw)
                        if raw_total > self.max_bytes:
                            raw = raw[: len(raw) - (raw_total - self.max_bytes)]
                            clipped = True
                        decoded, hit_limit = decoder.feed(raw, self.max_bytes - total)
                        total += len(decoded)
                        chunks.append(decoded)
                        if hit_limit or clipped:
                            clipped = True
                            break
                    return b"".join(chunks), content_type or "text/html", charset, clipped
        except FetchError:
            raise
        except httpx.TimeoutException:
            raise FetchError(FetchErrorKind.TIMEOUT, "The site did not answer in time.") from None
        except httpx.HTTPError:
            raise FetchError(FetchErrorKind.NETWORK, "Could not connect to the site.") from None
        except (OSError, ValueError, UnicodeError):
            raise FetchError(FetchErrorKind.NETWORK, "Could not connect to the site.") from None

    def _page(self, url: str, body: bytes, content_type: str, charset: str, clipped: bool,
              redirects: list[str]) -> Page:
        if not body.strip():
            raise FetchError(FetchErrorKind.EMPTY, "The page was empty.")
        text_source = ""
        for encoding in filter(None, (charset, "utf-8")):
            try:
                text_source = body.decode(encoding, errors="replace")
                break
            except LookupError:
                continue
        if content_type in ("text/html", "application/xhtml+xml"):
            sniff = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", body[:4096], re.IGNORECASE)
            if sniff and not charset:
                try:
                    text_source = body.decode(sniff.group(1).decode("ascii"), errors="replace")
                except LookupError:
                    pass
            title, text, truncated = extract_text(text_source, self.max_chars)
        else:
            text = text_source[: self.max_chars]
            truncated = len(text_source) > self.max_chars
            title = ""
        text = text.strip()
        if title and title.lower() not in text[:300].lower():
            text = f"# {title}\n{text}"
        if len(text) < 40:
            raise FetchError(FetchErrorKind.EMPTY,
                             "The page has almost no readable text (it may need JavaScript or a sign-in).")
        return Page(url, title, text, truncated or clipped, content_type, len(body), redirects)
