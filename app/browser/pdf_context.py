"""PDF text extraction, with page numbers, for a PDF open in a tab.

QtWebEngine renders a PDF using Chromium's own built-in viewer plugin (see
``BrowserProfile``'s ``PdfViewerEnabled``) - a real page in every sense
except that its content is not DOM text the way an HTML page's is. To let
Py read one, this module fetches the same bytes the viewer is displaying
(a local file read for ``file://``, an HTTP GET otherwise) and parses them
with ``pypdf`` directly - independent of whatever the viewer is doing on
screen, and the only way to get real page numbers back at all.

No OCR here. A scanned (image-only) PDF is reported as having no
extractable text - see ``PdfDocument.is_scanned`` - rather than silently
guessing at it or claiming a capability this module does not have.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from urllib.parse import urlsplit

#: A page's extracted text is capped this long - a badly-formatted PDF can
#: extract to megabytes of near-garbage per page, and the agent's own
#: context limits would just truncate it anyway. Better to say so here,
#: at the source, than let it silently eat the conversation budget.
MAX_PAGE_CHARS = 8000
#: Pages read per document. A 300-page report is not going to be sent to a
#: model in one shot regardless; this is a hard ceiling so extraction
#: itself cannot run away on a huge file.
MAX_PAGES = 200


def is_pdf_url(url: str) -> bool:
    """Cheap and honest: a PDF is whatever ends in ``.pdf`` - no content-type
    sniffing, no guessing. Good enough for "is this tab a PDF", which is
    all this is used for."""
    path = urlsplit(url).path.lower()
    return path.endswith(".pdf")


@dataclass(frozen=True)
class PdfPage:
    number: int   # 1-based, because that's how a person refers to a page
    text: str


@dataclass(frozen=True)
class PdfDocument:
    source: str
    title: str
    pages: tuple[PdfPage, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        """Every page's text, each one labelled - so a citation like "page
        7 says..." is something the model can actually produce, not guess
        at from an undifferentiated blob."""
        return "\n\n".join(f"[Page {page.number}]\n{page.text}"
                          for page in self.pages if page.text.strip())

    @property
    def is_scanned(self) -> bool:
        """True when not one page yielded any extractable text - almost
        always a scanned/image-only PDF. This module does not OCR; the
        caller should say that plainly rather than return silence."""
        return bool(self.pages) and not any(page.text.strip() for page in self.pages)

    def search(self, query: str) -> list[tuple[int, str]]:
        """(page_number, a short snippet around the match) for every page
        containing ``query`` (case-insensitive). Cheap substring search,
        not an index - this is "search inside this one PDF", not a corpus."""
        needle = query.strip().lower()
        if not needle:
            return []
        results = []
        for page in self.pages:
            lowered = page.text.lower()
            pos = lowered.find(needle)
            if pos == -1:
                continue
            start = max(0, pos - 60)
            end = min(len(page.text), pos + len(query) + 60)
            snippet = page.text[start:end].strip()
            results.append((page.number, snippet))
        return results


class PdfExtractionError(RuntimeError):
    """Always carries a message meant to be shown to a person - never the
    raw underlying exception, which can quote file paths or, for a fetched
    URL, response internals."""


def _read_bytes(source: str, *, timeout: float = 20.0) -> bytes:
    if source.startswith("file://"):
        from PySide6.QtCore import QUrl

        path = QUrl(source).toLocalFile()
        if not path:
            raise PdfExtractionError(f"Not a readable local file: {source}")
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise PdfExtractionError(f"Could not read that file: {exc}") from exc
    if source.startswith(("http://", "https://")):
        import httpx2 as httpx

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(source)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            raise PdfExtractionError(f"Could not fetch that PDF: {exc}") from exc
    raise PdfExtractionError(f"Don't know how to read a PDF from this address: {source}")


def extract_pdf(source: str, *, max_pages: int = MAX_PAGES,
                data: bytes | None = None) -> PdfDocument:
    """Fetch (unless ``data`` is given directly - a local-file upload
    already has the bytes) and parse a PDF. Raises ``PdfExtractionError``,
    with a message safe to show a user, on anything that goes wrong -
    an unreadable source, a corrupt file, or the ``pypdf`` dependency
    missing.
    """
    if data is None:
        data = _read_bytes(source)

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfExtractionError(
            "PDF text extraction needs the 'pypdf' package, which is not "
            "installed.") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - pypdf's own errors vary by failure mode
        raise PdfExtractionError(f"That doesn't look like a valid PDF: {exc}") from exc

    pages = []
    for index, page in enumerate(reader.pages[:max_pages]):
        try:
            text = (page.extract_text() or "")[:MAX_PAGE_CHARS]
        except Exception:  # noqa: BLE001 - one bad page must not fail the whole document
            text = ""
        pages.append(PdfPage(number=index + 1, text=text))

    title = ""
    try:
        metadata = reader.metadata
        if metadata is not None and metadata.title:
            title = str(metadata.title)
    except Exception:  # noqa: BLE001 - metadata is a nicety, never load-bearing
        pass

    return PdfDocument(source=source, title=title, pages=tuple(pages))
