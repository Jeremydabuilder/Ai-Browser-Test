"""The Qt side of the Team: owns the running engine and feeds the UI.

Everything here lives on the GUI thread. The engine runs on one plain Python
thread (``team-engine``) and talks back through ``GuiDispatcher`` - the
repo's thread-safe queue + GUI-owned timer primitive (see app/gui_dispatch.py
for why no Qt signal ever crosses that boundary carrying objects). Worker
threads never touch a Qt object: even the local-knowledge search the
Researcher uses is marshalled to the GUI thread with ``run_sync``.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.gui_dispatch import GuiDispatcher, GuiDispatchShutdown
from app.team import sandbox as sandbox_mod
from app.team.engine import Capabilities, TeamEngine
from app.team.limits import TeamLimits
from app.team.llm import ProviderStatus, make_client_factory, resolve_provider
from app.team.model import Mission, MissionStatus, Source, SourceKind, SourceStatus
from app.team.workspace import Workspace, WorkspaceError

MAX_SOURCES = 8
MAX_SOURCE_CHARS = 20_000
_READABLE_SCHEMES = ("http://", "https://", "file://")


def next_source_id(sources: list[Source]) -> str:
    numbers = [int(s.id[1:]) for s in sources if s.id[1:].isdigit()]
    return f"S{max(numbers, default=0) + 1}"


def paste_source(sources: list[Source], text: str, title: str = "Pasted text") -> Source:
    clean = text.strip()
    source = Source(next_source_id(sources), SourceKind.PASTE, title, "", clean[:MAX_SOURCE_CHARS])
    if len(clean) > MAX_SOURCE_CHARS:
        source.status = SourceStatus.TRUNCATED
    if not clean:
        source.status, source.error = SourceStatus.INACCESSIBLE, "The pasted text is empty."
    return source


def file_source(sources: list[Source], path: str) -> Source:
    """Read a user-chosen file with the browser's own file reader."""
    from pathlib import Path

    from app.browser.file_context import FileParsingError, read_local_file

    name = Path(path).name
    try:
        document = read_local_file(path)
    except FileParsingError as exc:
        return Source(next_source_id(sources), SourceKind.FILE, name, "", "",
                      SourceStatus.INACCESSIBLE, str(exc))
    text = document.text[:MAX_SOURCE_CHARS]
    status = SourceStatus.TRUNCATED if document.truncated or len(document.text) > MAX_SOURCE_CHARS \
        else SourceStatus.INCLUDED
    return Source(next_source_id(sources), SourceKind.FILE, document.filename, "", text, status,
                  "" if text.strip() else "The file has no extractable text.")


def collect_tab_sources(browser, tab_ids: list[int], sources: list[Source],
                        done: Callable[[list[Source]], None]) -> None:
    """Read the chosen tabs through the browser's existing page extraction
    (get_page_text / get_pdf_text). Calls ``done`` once, on the GUI thread,
    with one Source per tab - unreadable pages come back INACCESSIBLE with the
    reason, never dropped silently."""
    if not tab_ids:
        done([])
        return
    tabs = {t["tab_id"]: t for t in browser.list_tabs()}
    results: dict[int, Source] = {}
    taken = list(sources)

    def make(tab_id: int, text: str, ok: bool, error: str, truncated: bool = False) -> Source:
        tab = tabs.get(tab_id, {})
        status = SourceStatus.INCLUDED
        if not ok or not text.strip():
            status = SourceStatus.INACCESSIBLE
            error = error or "The page has no readable text (it may still be loading, blank, or need sign-in)."
        elif truncated or len(text) > MAX_SOURCE_CHARS:
            status = SourceStatus.TRUNCATED
        readable = ok and bool(text.strip())
        source = Source(next_source_id(taken), SourceKind.TAB, tab.get("title") or tab.get("url") or "Tab",
                        tab.get("url", ""), text[:MAX_SOURCE_CHARS] if readable else "", status, error)
        taken.append(source)
        return source

    def finish_one(tab_id: int, source: Source) -> None:
        results[tab_id] = source
        if len(results) == len(tab_ids):
            done([results[t] for t in tab_ids])

    for tab_id in tab_ids:
        tab = tabs.get(tab_id)
        if tab is None:
            finish_one(tab_id, make(tab_id, "", False, "That tab was closed."))
            continue
        url = str(tab.get("url", ""))
        if not url.lower().startswith(_READABLE_SCHEMES):
            finish_one(tab_id, make(tab_id, "", False, "Browser-internal or unsupported page; not readable."))
            continue
        if url.lower().split("?")[0].endswith(".pdf"):
            result = browser.get_pdf_text(tab_id)
            data = result.data if result.ok else {}
            error = result.error.message if (not result.ok and result.error) else ""
            finish_one(tab_id, make(tab_id, str(data.get("text", "")), result.ok, error,
                                    bool(data.get("truncated"))))
            continue

        def on_text(result, tab_id=tab_id) -> None:
            data = result.data if result.ok else {}
            error = result.error.message if (not result.ok and result.error) else ""
            finish_one(tab_id, make(tab_id, str(data.get("text", "")), result.ok, error,
                                    bool(data.get("truncated"))))

        browser.get_page_text(tab_id, max_chars=MAX_SOURCE_CHARS).then(on_text)


def knowledge_search_adapter(knowledge, dispatcher: GuiDispatcher):
    """Search the local knowledge index (history, missions, highlights, files)
    for the Researcher. Returns None when the index is off - the Team then
    says search is unavailable rather than pretending."""
    if knowledge is None or not getattr(knowledge, "enabled", False):
        return None

    def search(query: str) -> list[dict[str, str]]:
        def run() -> list[dict[str, str]]:
            from app.knowledge.retrieval import search as retrieve

            results = retrieve(knowledge.store.all_chunks(), query, limit=5)
            return [{"title": r.chunk.title or "Local knowledge", "url": r.chunk.location or "",
                     "excerpt": r.excerpt} for r in results]

        return dispatcher.run_sync(run, timeout=10) or []

    return search


class TeamController(QObject):
    """Owns the current Team run. Parent it to the window (a long-lived
    GUI-thread QObject) so its dispatcher timer is torn down on that thread."""

    changed = Signal()
    history_changed = Signal()

    def __init__(self, store, settings=None, knowledge=None, parent: QObject | None = None,
                 *, client_factory: Callable[[], Any] | None = None) -> None:
        """``client_factory`` is a test seam only: when given, it replaces the
        real provider client (and the credential check). The UI never sets it."""
        super().__init__(parent)
        self._client_factory = client_factory
        self._store = store
        self._settings = settings
        self._knowledge = knowledge
        self._dispatcher = GuiDispatcher(parent=self)
        self._engine: TeamEngine | None = None
        self._thread: threading.Thread | None = None
        self._view: Mission | None = None
        self._workspace_path = ""
        self._pending = False
        self._pending_lock = threading.Lock()
        self._sandbox: sandbox_mod.SandboxStatus | None = None
        self._sandbox_key: bool | None = None
        self._closed = False
        if store is not None:
            try:
                if store.recover_after_restart():
                    self.history_changed.emit()
            except Exception:  # noqa: BLE001 - history is never load-bearing
                pass

    # -- environment ----------------------------------------------------------
    @property
    def settings(self):
        return self._settings

    def limits(self) -> TeamLimits:
        return TeamLimits.from_settings(self._settings)

    def provider_status(self) -> ProviderStatus:
        if self._client_factory is not None:
            return ProviderStatus("groq", "Test provider", "scripted", True, "injected test client")
        return resolve_provider(self._settings)

    def sandbox_status(self) -> sandbox_mod.SandboxStatus:
        allow = False
        try:
            allow = bool(self._settings.get_bool("team_allow_unisolated_execution", False))
        except Exception:  # noqa: BLE001
            allow = False
        if self._sandbox is None or self._sandbox_key != allow:
            self._sandbox = sandbox_mod.probe(allow_unisolated=allow)
            self._sandbox_key = allow
        return self._sandbox

    def workspace_for(self, path: str) -> Workspace | None:
        if not path:
            return None
        try:
            return Workspace(path)
        except WorkspaceError:
            return None

    def capabilities(self, workspace_path: str = "") -> Capabilities:
        return Capabilities(
            sandbox=self.sandbox_status(), workspace=self.workspace_for(workspace_path),
            search=knowledge_search_adapter(self._knowledge, self._dispatcher))

    # -- state ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._engine is not None and self._engine.running

    def snapshot(self) -> Mission | None:
        if self._engine is not None:
            return self._engine.snapshot()
        return self._view

    def history(self):
        return self._store.history() if self._store is not None else []

    # -- actions ------------------------------------------------------------------
    def start(self, goal: str, sources: list[Source], workspace_path: str = "") -> Mission | None:
        if self.is_running or self._closed:
            return None
        status = self.provider_status()
        mission = Mission(goal=goal.strip(), sources=list(sources), status=MissionStatus.PLANNING,
                          workspace_path=workspace_path, model_label=f"{status.label} · {status.model}".strip(" ·"))
        if self._store is not None:
            self._store.create(mission)
        self._launch(mission, status)
        self.history_changed.emit()
        return mission

    def retry(self) -> bool:
        """Continue the current mission from where it stopped."""
        mission = self.snapshot()
        if mission is None or self.is_running or self._closed:
            return False
        if mission.status not in (MissionStatus.FAILED, MissionStatus.CANCELLED, MissionStatus.INTERRUPTED,
                                  MissionStatus.COMPLETED_WITH_ISSUES):
            return False
        self._launch(mission, self.provider_status())
        return True

    def open_mission(self, mission_id: int) -> Mission | None:
        if self.is_running or self._store is None:
            return None
        mission = self._store.load(mission_id)
        if mission is None:
            return None
        self._engine = None
        self._view = mission
        self._workspace_path = mission.workspace_path
        self.changed.emit()
        return mission

    def delete_mission(self, mission_id: int) -> None:
        if self._store is None or (self.is_running and self._view and self._view.id == mission_id):
            return
        self._store.delete(mission_id)
        if self._view is not None and self._view.id == mission_id:
            self._view = None
            self._engine = None
        self.history_changed.emit()
        self.changed.emit()

    def new_mission(self) -> None:
        if not self.is_running:
            self._engine = None
            self._view = None
            self.changed.emit()

    def cancel(self) -> None:
        if self._engine is not None and self.is_running:
            self._engine.cancel()

    def apply_files(self, artifact_ids: list[str]) -> list[str]:
        if self._engine is None:
            mission = self._view
            if mission is None:
                raise WorkspaceError("No mission is open.")
            workspace = self.workspace_for(mission.workspace_path)
            engine = TeamEngine(mission, lambda: None, self.limits(),
                                Capabilities(self.sandbox_status(), workspace=workspace), store=self._store)
            written = engine.apply_files(artifact_ids)
            self.changed.emit()
            return written
        written = self._engine.apply_files(artifact_ids)
        self.changed.emit()
        return written

    # -- plumbing ---------------------------------------------------------------------
    def _launch(self, mission: Mission, status: ProviderStatus) -> None:
        limits = self.limits()
        factory = self._client_factory or make_client_factory(status, self._settings, limits)
        engine = TeamEngine(
            mission, factory, limits,
            self.capabilities(mission.workspace_path), store=self._store,
            on_change=self._on_engine_change, secret=status.secret, provider_label=status.label)
        self._engine = engine
        self._view = None
        thread = threading.Thread(target=self._run, args=(engine,), name="team-engine")
        self._thread = thread
        thread.start()
        self.changed.emit()

    @staticmethod
    def _run(engine: TeamEngine) -> None:
        engine.run()

    def _on_engine_change(self, _kind: str) -> None:
        """Called on engine/worker threads: coalesce and bounce to the GUI thread."""
        with self._pending_lock:
            if self._pending:
                return
            self._pending = True
        try:
            self._dispatcher.post(self._deliver)
        except GuiDispatchShutdown:
            pass

    def _deliver(self) -> None:
        with self._pending_lock:
            self._pending = False
        if self._closed:
            return
        self.changed.emit()
        if self._engine is not None and not self._engine.running:
            self.history_changed.emit()

    def shutdown(self) -> None:
        """Stop the run and the dispatcher. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._engine is not None:
            self._engine.cancel()
        self._dispatcher.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=8)
