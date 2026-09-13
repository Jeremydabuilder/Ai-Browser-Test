"""Universal @context: one selection model behind the @-mention composer.

Phase 3 built PDF, local-file and image context as three small, independent
flows, each triggered by its own explicit user action (a menu item, a native
file dialog). That was deliberate at the time - each flow needed nothing
from the others. Phase 4 adds a single composer where a person picks several
of those same things at once (a tab, a PDF tab, a Mission, an MCP tool, a
file, an image) before asking Py anything - which is the point past which a
shared shape actually earns its keep: one list of "what's selected", one
place that enforces "Py sees only this, not the whole browser", one place
that checks whether an image can actually be sent before Py ever hears
about it.

This module deliberately does NOT change how each context type is actually
read: a tab's text still comes from browser_get_page_text/browser_get_pdf_text
(Py calls the tool - see _tool_hint), a file/image's bytes are still resolved
once, locally, at selection time (see app/browser/file_context.py and
app/browser/image_context.py), exactly as Phase 3 left them. ContextComposer
is a selection and prompt-assembly layer on top, not a fourth way to fetch
content.

No Qt import here - this is deliberately unit-testable without a QApplication,
the same split tab_grouping.py's suggestion engine uses against its dialog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import wrap_untrusted
from app.security.provenance import Provenance
from app.browser.pdf_context import is_pdf_url

#: Selected items' fenced text is capped in total, not just per item - a
#: person can select several tabs and a file at once, and the model's own
#: context budget would truncate the combination anyway. Better to say so
#: here, once, than let each item's own generous individual cap add up.
MAX_COMPOSED_CHARS = 20000

#: The two items that do not come from a live list - selecting either one
#: opens a native picker (see app/ui/main_window.py's existing
#: _ask_py_about_local_file / _ask_py_about_local_image) rather than
#: resolving to something already open.
ACTION_FILE = "action:file"
ACTION_IMAGE = "action:image"
#: Phase 13 - selecting this does not open a picker; it marks that the
#: message the user is about to type should also be used as a semantic-
#: search query over the local knowledge index (history, Missions,
#: findings, highlights, PDFs, files) at build() time - see
#: ContextComposer.build's "knowledge" branch.
ACTION_KNOWLEDGE = "action:knowledge"


@dataclass(frozen=True)
class ContextItem:
    """One thing selected for Py to use - a tab, a Mission, a file already
    read, an image already captured, an MCP tool, or one of the two picker
    actions above.

    ``ref`` carries whatever is needed to use the item: {"tab_id": ...} for
    a tab, {"mission_id": ...} for a Mission, {"text": ..., "truncated": ...}
    for an already-parsed file, {"mime_type": ..., "data": ...} for an
    already-encoded image, {"server_id": ..., "tool_name": ...} for an MCP
    tool. Never a raw filesystem path the model could act on by itself.
    """

    id: str
    kind: str       # "tab" | "pdf_tab" | "mission" | "file" | "image" | "mcp_tool" | action ids
    title: str
    subtitle: str = ""
    requires_vision: bool = False
    ref: dict[str, Any] = field(default_factory=dict)


def _tool_hint(url: str) -> str:
    return "browser_get_pdf_text" if is_pdf_url(url) else "browser_get_page_text"


class ContextComposer:
    """Builds the @-mention candidate list, holds the current selection, and
    assembles one prompt from it.

    Constructed once per AgentPanel (like the panel's other collaborators);
    ``browser``/``missions``/``mcp`` are the same objects MainWindow already
    owns, passed in rather than looked up, so this stays as testable as
    ToolRegistry - given fakes, no Qt app needed at all for the tab/mission/
    mcp candidate paths (file/image resolution does touch the filesystem,
    covered separately in tests/test_file_context.py and
    tests/test_image_context.py; this module reuses those functions rather
    than re-implementing them).
    """

    def __init__(self, *, browser=None, missions=None, mcp=None, highlights=None,
                 knowledge=None) -> None:
        self._browser = browser
        self._missions = missions
        self._mcp = mcp
        #: HighlightStore, owned by MainWindow like the others - None where
        #: no window context exists (a bare test), in which case @highlight
        #: simply lists nothing rather than failing.
        self._highlights = highlights
        #: Phase 13 KnowledgeIndex, or None. The @knowledge candidate only
        #: appears when both this is given AND the index is enabled - an
        #: off-by-default feature should not even offer itself in the
        #: composer while the user has not opted in.
        self._knowledge = knowledge
        self._selected: dict[str, ContextItem] = {}

    # -- candidates --------------------------------------------------------
    def available_items(self) -> list[ContextItem]:
        """Everything that could be @-mentioned right now, freshly computed -
        never cached, so a closed tab or a finished Mission simply stops
        appearing rather than needing to be actively removed from a list."""
        items: list[ContextItem] = []
        items.extend(self._tab_items())
        items.extend(self._mission_items())
        items.extend(self._mcp_items())
        items.extend(self._highlight_items())
        if self._knowledge is not None and self._knowledge.enabled:
            items.append(ContextItem(
                id=ACTION_KNOWLEDGE, kind=ACTION_KNOWLEDGE,
                title="Search Knowledge & History…",
                subtitle="Retrieves relevant past pages/Missions/highlights for your question"))
        items.append(ContextItem(id=ACTION_FILE, kind=ACTION_FILE,
                                 title="Attach a local file…",
                                 subtitle="Opens a file picker"))
        items.append(ContextItem(id=ACTION_IMAGE, kind=ACTION_IMAGE,
                                 title="Attach an image…",
                                 subtitle="Opens a file picker, or use "
                                          "Tools → Ask Py about a Screenshot"))
        return items

    def _tab_items(self) -> list[ContextItem]:
        if self._browser is None:
            return []
        items = []
        for row in self._browser.list_tabs():
            url = row.get("url", "")
            pdf = is_pdf_url(url)
            items.append(ContextItem(
                id=f"tab:{row['tab_id']}", kind="pdf_tab" if pdf else "tab",
                title=row.get("title") or "Untitled tab", subtitle=url,
                ref={"tab_id": row["tab_id"], "url": url}))
        return items

    def _mission_items(self) -> list[ContextItem]:
        if self._missions is None:
            return []
        items = []
        active = self._missions.active
        for mission in self._missions.recent(limit=10):
            label = "Active Mission" if active is not None and mission.id == active.id else "Mission"
            items.append(ContextItem(
                id=f"mission:{mission.id}", kind="mission",
                title=mission.title or "Untitled Mission", subtitle=label,
                ref={"mission_id": mission.id}))
        return items

    def _highlight_items(self) -> list[ContextItem]:
        """A saved highlight is already fully self-contained (its own copy
        of url/title/text/note) - see app/storage/highlights.py - so unlike
        a tab this never needs a live page to still be useful as context."""
        if self._highlights is None:
            return []
        items = []
        for highlight in self._highlights.all():
            subtitle = highlight.url or "(no source)"
            if highlight.note:
                subtitle = f"{subtitle} - {highlight.note}"
            items.append(ContextItem(
                id=f"highlight:{highlight.id}", kind="highlight",
                title=highlight.title or "Untitled highlight", subtitle=subtitle,
                ref={"text": highlight.text, "url": highlight.url,
                     "title": highlight.title, "note": highlight.note}))
        return items

    def _mcp_items(self) -> list[ContextItem]:
        if self._mcp is None:
            return []
        items = []
        for config in self._mcp.configured_servers():
            for tool in self._mcp.all_tools(config.id):
                items.append(ContextItem(
                    id=f"mcp:{config.id}:{tool.name}", kind="mcp_tool",
                    title=tool.name, subtitle=config.name,
                    ref={"server_id": config.id, "tool_name": tool.name}))
        return items

    def search(self, query: str) -> list[ContextItem]:
        """Candidates matching ``query`` (whatever the user typed after
        "@"), by simple case-insensitive substring match against the
        title/kind/subtitle - fuzzy enough for a short prefix, honest about
        not being a ranked search engine."""
        needle = (query or "").strip().lower()
        candidates = self.available_items()
        if not needle:
            return candidates
        return [item for item in candidates
                if needle in item.title.lower() or needle in item.kind.lower()
                or needle in item.subtitle.lower()]

    # -- selection -----------------------------------------------------
    @property
    def selected(self) -> list[ContextItem]:
        return list(self._selected.values())

    def add(self, item: ContextItem) -> None:
        """Adding an item already selected (same id) is a no-op - the whole
        point of keying by id rather than appending to a list."""
        self._selected[item.id] = item

    def add_file(self, path: str):
        """Resolve a local file the user just picked and select it.
        Raises FileParsingError (from app.browser.file_context) exactly as
        the Phase 3 flow does - the caller shows it the same way."""
        from app.browser.file_context import read_local_file

        document = read_local_file(path)
        item = ContextItem(id=f"file:{document.source}", kind="file",
                           title=document.filename, subtitle=document.source,
                           ref={"text": document.text, "truncated": document.truncated})
        self.add(item)
        return item

    def add_image_file(self, path: str):
        """Resolve a local image the user just picked and select it. Raises
        ImageContextError exactly as the Phase 3 flow does."""
        from app.browser.image_context import read_local_image

        attachment = read_local_image(path)
        item = ContextItem(id=f"image:{attachment.source}", kind="image",
                           title=attachment.filename, subtitle=attachment.source,
                           requires_vision=True,
                           ref={"mime_type": attachment.mime_type, "data": attachment.base64})
        self.add(item)
        return item

    def add_screenshot(self, attachment) -> ContextItem:
        """Select an already-captured screenshot (see
        app.browser.image_context.screenshot_attachment / MainWindow's
        existing capture flow) - this method does not touch Qt itself."""
        item = ContextItem(id=f"image:{attachment.source}", kind="image",
                           title=attachment.filename, subtitle=attachment.source,
                           requires_vision=True,
                           ref={"mime_type": attachment.mime_type, "data": attachment.base64})
        self.add(item)
        return item

    def remove(self, item_id: str) -> None:
        self._selected.pop(item_id, None)

    def clear(self) -> None:
        self._selected.clear()

    # -- capability ------------------------------------------------------
    def vision_conflicts(self, provider_supports_images: bool) -> list[ContextItem]:
        """Selected items that need vision but cannot be sent right now -
        never silently dropped from the selection (see build()) and never
        silently sent either; the caller shows these as a warning banner."""
        if provider_supports_images:
            return []
        return [item for item in self._selected.values() if item.requires_vision]

    # -- prompt assembly ---------------------------------------------------
    def build(self, user_text: str, *, provider_supports_images: bool
             ) -> tuple[str, dict[str, str] | None]:
        """Combine the current selection with what the user typed into one
        message, plus at most one image attachment (AgentSession.send's
        ``image`` parameter carries a single image - selecting several image
        items sends the first and says so about the rest, rather than
        silently dropping them or claiming multi-image support this session
        does not have).

        A tab or PDF tab is named for Py to read with the matching tool
        (browser_get_page_text / browser_get_pdf_text) - reusing the
        existing tool-call path rather than eagerly fetching page text here,
        which would need to block on a BrowserFuture outside the Qt event
        loop this composer has no access to. A file or image was already
        resolved at selection time (add_file/add_image_file/add_screenshot),
        so its content is embedded directly, exactly as the Phase 3 flows do.
        """
        items = self.selected
        if not items:
            return user_text, None

        lines: list[str] = [
            "Use ONLY the following selected context to answer, unless I "
            "explicitly ask you to look at something broader:"]
        budget = MAX_COMPOSED_CHARS
        image: dict[str, str] | None = None
        image_used = False

        for item in items:
            if item.kind in ("tab", "pdf_tab"):
                url = item.ref.get("url", "")
                lines.append(
                    f"- Tab (tab_id={item.ref.get('tab_id')}): \"{item.title}\" ({url}) "
                    f"- read with {_tool_hint(url)}")
            elif item.kind == "mission":
                lines.append(
                    f"- Mission (mission_id={item.ref.get('mission_id')}): "
                    f"\"{item.title}\" - use the mission_* tools for its findings/goal")
            elif item.kind == "mcp_tool":
                lines.append(
                    f"- MCP tool available: {item.ref.get('server_id')}/"
                    f"{item.ref.get('tool_name')}")
            elif item.kind == "file":
                text = item.ref.get("text", "")
                if len(text) > budget:
                    text = text[:budget] + "\n[truncated to fit the context budget]"
                budget = max(0, budget - len(text))
                lines.append(f'- File "{item.title}" ({item.subtitle}):')
                lines.append(wrap_untrusted({"file_text": text}, provenance=Provenance.FILE))
            elif item.kind == "highlight":
                text = item.ref.get("text", "")
                if len(text) > budget:
                    text = text[:budget] + "\n[truncated to fit the context budget]"
                budget = max(0, budget - len(text))
                url = item.ref.get("url", "")
                lines.append(f'- Highlight from "{item.title}" ({url}):')
                payload = {"highlighted_text": text}
                note = item.ref.get("note", "")
                if note:
                    payload["note"] = note
                lines.append(wrap_untrusted(payload))
            elif item.kind == ACTION_KNOWLEDGE:
                lines.append(self._knowledge_block(user_text))
            elif item.kind == "image":
                if not provider_supports_images:
                    lines.append(
                        f'- Image "{item.title}" is selected but the configured '
                        "provider does not support images - it was not sent. "
                        "Switch provider to include it.")
                elif image_used:
                    lines.append(
                        f'- Image "{item.title}" was NOT sent - only one image '
                        "can be attached per message right now.")
                else:
                    image = {"mime_type": item.ref.get("mime_type", "image/png"),
                             "data": item.ref.get("data", "")}
                    image_used = True
                    lines.append(f'- Image "{item.title}" is attached to this message.')

        combined = "\n".join(lines) + "\n\n" + user_text
        return combined, image

    #: At most this many retrieved chunks are ever handed to the model -
    #: "select only relevant chunks", never the whole index.
    MAX_KNOWLEDGE_RESULTS = 5

    def _knowledge_block(self, query: str) -> str:
        """Retrieve relevant chunks for ``query`` (the message the user is
        about to send) and fence them as untrusted, provenance-labeled
        excerpts - never the raw index, never unlabeled text a model could
        mistake for the user's own words or for current fact regardless of
        age (see each result's ``stale`` flag)."""
        if self._knowledge is None or not query.strip():
            return "- @knowledge: no query text to search for."
        from app.knowledge.retrieval import search
        from app.knowledge.types import SourceType

        chunks = self._knowledge.store.all_chunks()
        results = search(chunks, query, limit=self.MAX_KNOWLEDGE_RESULTS)
        if not results:
            return "- @knowledge: no relevant local history/Mission/file content found."
        lines = ["- Relevant local knowledge (your own browsing history, Missions, "
                "findings, highlights, PDFs and files - fenced as untrusted data below):"]
        for result in results:
            chunk = result.chunk
            label = SourceType.LABELS.get(chunk.source_type, chunk.source_type)
            freshness = " (may be stale - indexed a while ago)" if result.stale else ""
            payload = {
                "source_type": label, "title": chunk.title, "location": chunk.location,
                "timestamp": chunk.timestamp, "excerpt": result.excerpt,
            }
            lines.append(f"  {label}{freshness}:")
            lines.append("  " + wrap_untrusted(payload, provenance=Provenance.KNOWLEDGE_RETRIEVAL))
        return "\n".join(lines)


def context_icon(kind: str) -> str:
    """A short plain-text glyph per kind, for a chip or a candidate row -
    deliberately just a word, not an emoji-per-kind guessing game."""
    return {
        "tab": "TAB", "pdf_tab": "PDF", "mission": "MISSION", "mcp_tool": "MCP",
        "file": "FILE", "image": "IMAGE", "highlight": "HIGHLIGHT",
        ACTION_FILE: "+FILE", ACTION_IMAGE: "+IMAGE", ACTION_KNOWLEDGE: "KNOWLEDGE",
    }.get(kind, "?")
