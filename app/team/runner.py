"""The Qt side of the Team: owns the running engine and feeds the UI.

Everything here lives on the GUI thread. The engine runs on one plain Python
thread (``team-engine``) and talks back through ``GuiDispatcher`` - the
repo's thread-safe queue + GUI-owned timer primitive (see app/gui_dispatch.py
for why no Qt signal ever crosses that boundary carrying objects). Worker
threads never touch a Qt object: even the local-knowledge search the
Researcher uses is marshalled to the GUI thread with ``run_sync``.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.gui_dispatch import GuiDispatcher, GuiDispatchShutdown
from app.team import sandbox as sandbox_mod
from app.team import websearch
from app.team.engine import Capabilities, TeamEngine
from app.team.limits import TeamLimits
from app.team.llm import TeamError, ProviderStatus, make_client_factory, resolve_provider
from app.team.model import Mission, MissionStatus, Source, SourceKind, SourceStatus
from app.team.webfetch import PageFetcher
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
    #: The sandbox probe finished (or settings changed) - re-read the environment.
    environment_changed = Signal()
    #: A generated file was written through the Downloads manager (message for a notice).
    file_saved = Signal(str)
    #: A follow-up question failed (message is safe to show) - or finished (empty string).
    followup_finished = Signal(str)

    def __init__(self, store, settings=None, knowledge=None, parent: QObject | None = None,
                 *, client_factory: Callable[[], Any] | None = None, downloads=None,
                 downloads_dir: str | None = None, search_transport=None, page_fetcher=None) -> None:
        """``client_factory`` and ``search_transport`` are test seams only: they
        replace the real provider client / search HTTP transport. The UI never
        sets them. ``downloads`` is the profile's DownloadManager."""
        super().__init__(parent)
        self._client_factory = client_factory
        self._downloads = downloads
        self._downloads_dir = downloads_dir
        self._search_transport = search_transport
        self._page_fetcher = page_fetcher
        self._sandbox_done = threading.Event()
        self._sandbox_thread: threading.Thread | None = None
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
        self._asking = False
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

    def _sandbox_params(self) -> tuple[str, str]:
        def get(key: str, default: str) -> str:
            try:
                return (self._settings.get(key, "") or default).strip() or default
            except Exception:  # noqa: BLE001
                return default
        return get("team_sandbox_backend", "auto"), get("team_container_image", sandbox_mod.DEFAULT_IMAGE)

    def sandbox_status(self) -> sandbox_mod.SandboxStatus:
        """Never blocks the GUI: returns the cached, VERIFIED status or a
        'checking' placeholder while a background probe (which may start a
        container) runs. ``environment_changed`` fires when it finishes."""
        key = self._sandbox_params()
        if self._sandbox is not None and self._sandbox_key == key:
            return self._sandbox
        if self._sandbox_thread is None or not self._sandbox_thread.is_alive() or self._sandbox_key != key:
            self._sandbox_key = key
            self._sandbox = None
            self._sandbox_done.clear()
            thread = threading.Thread(target=self._probe_sandbox, args=(key,), name="team-sandbox-probe")
            self._sandbox_thread = thread
            thread.start()
        return sandbox_mod.checking_status()

    def recheck_sandbox(self) -> None:
        """Forget the cached probe (after installing Docker, pulling an image...)."""
        sandbox_mod._cache.clear()
        self._sandbox = None
        self._sandbox_key = None
        self.sandbox_status()
        self.environment_changed.emit()

    def _probe_sandbox(self, key: tuple[str, str]) -> None:
        status = sandbox_mod.probe(key[0], key[1], limits=self.limits(), use_cache=False)
        if self._closed or key != self._sandbox_key:
            return
        self._sandbox = status
        self._sandbox_done.set()
        try:
            self._dispatcher.post(self.environment_changed.emit)
        except GuiDispatchShutdown:
            pass

    def await_sandbox(self, timeout: float = 90.0) -> sandbox_mod.SandboxStatus:
        """For the engine thread: wait for the probe, never the GUI thread."""
        self.sandbox_status()
        self._sandbox_done.wait(timeout)
        return self._sandbox or sandbox_mod.SandboxStatus(False, "the sandbox check did not finish in time")

    def web_status(self) -> websearch.SearchStatus:
        return websearch.resolve_search(self._settings)

    def web_search_client(self) -> websearch.WebSearch | None:
        status = self.web_status()
        if not status.available:
            return None
        return websearch.WebSearch(status, transport=self._search_transport)

    def workspace_for(self, path: str) -> Workspace | None:
        if not path:
            return None
        try:
            return Workspace(path)
        except WorkspaceError:
            return None

    def page_fetcher(self) -> PageFetcher | None:
        """Reads public pages for the Researcher; None when switched off (0 pages)."""
        limits = self.limits()
        if limits.max_fetch_pages <= 0:
            return None
        if self._page_fetcher is not None:
            return self._page_fetcher
        return PageFetcher(timeout=limits.fetch_timeout_s, deadline=limits.fetch_timeout_s * 2,
                           max_chars=limits.fetch_max_chars)

    def capabilities(self, workspace_path: str = "") -> Capabilities:
        web = self.web_search_client()
        return Capabilities(
            fetcher=self.page_fetcher() if web else None,
            sandbox=self._sandbox or sandbox_mod.checking_status(),
            workspace=self.workspace_for(workspace_path),
            search=knowledge_search_adapter(self._knowledge, self._dispatcher),
            web_search=web, web_search_note="" if web else self.web_status().detail)

    # -- state ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """A mission run OR a follow-up is in progress (either way nothing else may start)."""
        return (self._engine is not None and self._engine.running) or self._asking

    def snapshot(self) -> Mission | None:
        if self._engine is not None:
            return self._engine.snapshot()
        return self._view

    def history(self):
        return self._store.history() if self._store is not None else []

    # -- actions ------------------------------------------------------------------
    @property
    def followup_busy(self) -> bool:
        return self._asking

    def ask_followup(self, question: str, mode: str = "answer") -> bool:
        """Ask about the mission on screen (answer from evidence / rewrite / new research). Runs off the
        GUI thread; ``followup_finished`` fires with "" on success or a safe error message."""
        if self.is_running or self._asking or self._closed:
            return False
        mission = self._engine.mission if self._engine is not None else self._view
        if mission is None or not question.strip():
            return False
        status = self.provider_status()
        if not status.available:
            self.followup_finished.emit(status.detail)
            return False
        if self._engine is None or self._engine.mission is not mission:
            self._engine = self._new_engine(mission, status)
            self._view = None
        engine = self._engine
        self._asking = True
        self.changed.emit()

        def work() -> None:
            message = ""
            try:
                engine.ask(question, mode)
            except TeamError as exc:
                message = websearch_safe(exc.message, status.secret)
            except Exception as exc:  # noqa: BLE001 - never crash the app from a worker
                message = f"The follow-up failed ({type(exc).__name__})."
            self._post(lambda: self._followup_done(message))

        threading.Thread(target=work, name="team-followup", daemon=True).start()
        return True

    def _followup_done(self, message: str) -> None:
        self._asking = False
        if self._closed:
            return
        self.changed.emit()
        self.history_changed.emit()
        self.followup_finished.emit(message)

    def cancel_followup(self) -> None:
        if self._engine is not None and self._asking:
            self._engine.cancel_followup()

    def start(self, goal: str, sources: list[Source], workspace_path: str = "",
              web_search: bool = False, *, template_id: str = "",
              allowed_agents: tuple[str, ...] | list[str] = ()) -> Mission | None:
        if self.is_running or self._asking or self._closed:
            return None
        status = self.provider_status()
        mission = Mission(goal=goal.strip(), sources=list(sources), status=MissionStatus.PLANNING,
                          workspace_path=workspace_path, web_search=web_search,
                          template_id=template_id, allowed_agents=list(allowed_agents),
                          model_label=f"{status.label} · {status.model}".strip(" ·"))
        if self._store is not None:
            self._store.create(mission)
        self._launch(mission, status)
        self.history_changed.emit()
        return mission

    def retry(self, task_id: str = "", *, skip: bool = False, extra_round: bool = False) -> bool:
        """Continue the current mission from where it stopped. Finished work is
        kept. ``task_id`` retries only that failed task (and what it blocked);
        ``skip`` skips it instead; ``extra_round`` allows one more review round."""
        mission = self.snapshot()
        if mission is None or self.is_running or self._closed:
            return False
        if mission.status not in (MissionStatus.FAILED, MissionStatus.CANCELLED, MissionStatus.INTERRUPTED,
                                  MissionStatus.COMPLETED_WITH_ISSUES):
            return False
        if self._sandbox is not None and not self._sandbox.available:
            # The user may have started Docker since; a stale "unavailable" must not stick.
            sandbox_mod._cache.clear()
            self._sandbox = None
            self._sandbox_key = None
            self._sandbox_done.clear()
        kwargs: dict = {"extra_round": extra_round}
        if task_id:
            known = mission.task(task_id)
            if known is None:
                return False
            kwargs["skip" if skip else "retry_only"] = {task_id}
        self._launch(mission, self.provider_status(), **kwargs)
        return True

    @property
    def interrupted(self) -> bool:
        """The shown mission stopped before finishing (crash, restart, cancel, failure)."""
        view = self.snapshot()
        return view is not None and not self.is_running and view.status in (
            MissionStatus.INTERRUPTED, MissionStatus.CANCELLED, MissionStatus.FAILED)

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
        if self._asking:
            self.cancel_followup()
        elif self._engine is not None and self.is_running:
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
    def _new_engine(self, mission: Mission, status: ProviderStatus) -> TeamEngine:
        limits = self.limits()
        factory = self._client_factory or make_client_factory(status, self._settings, limits)
        engine = TeamEngine(
            mission, factory, limits,
            self.capabilities(mission.workspace_path), store=self._store,
            on_change=self._on_engine_change, secret=status.secret, provider_label=status.label)
        engine.capabilities.sandbox = self._sandbox or engine.capabilities.sandbox
        return engine

    def _launch(self, mission: Mission, status: ProviderStatus, **run_kwargs) -> None:
        engine = self._new_engine(mission, status)
        self._engine = engine
        self._view = None
        thread = threading.Thread(target=self._run, args=(engine,), kwargs=run_kwargs, name="team-engine")
        self._thread = thread
        thread.start()
        self.changed.emit()

    def _run(self, engine: TeamEngine, **run_kwargs) -> None:
        # The sandbox may still be proving itself; wait here, off the GUI thread.
        if self._sandbox is None:
            engine.capabilities.sandbox = self.await_sandbox()
        else:
            engine.capabilities.sandbox = self._sandbox
        engine.run(**run_kwargs)

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

    # -- connection tests (explicit user actions; run off the GUI thread) -----------
    def test_provider(self, done: Callable[[bool, str], None]) -> None:
        """One tiny real request with the configured key. ``done(ok, message)``
        is called on the GUI thread; the message never contains the key."""
        status = self.provider_status()

        def work() -> None:
            if not status.available:
                result = (False, status.detail)
            else:
                from app.agent.openai_compatible import (
                    GeminiClient, GroqClient, OpenAIClient, OpenRouterClient,
                )
                cls = {"groq": GroqClient, "openai": OpenAIClient, "openrouter": OpenRouterClient,
                       "gemini": GeminiClient}.get(status.provider)
                if cls is None:
                    result = (False, f"Cannot test provider '{status.provider}'.")
                else:
                    ok, message = cls.test_connection(status.secret, status.model)
                    result = (ok, websearch_safe(message, status.secret))
            self._post(lambda: done(*result))

        threading.Thread(target=work, name="team-test-provider", daemon=True).start()

    def test_search(self, done: Callable[[bool, str], None]) -> None:
        client = self.web_search_client()

        def work() -> None:
            if client is None:
                result = (False, self.web_status().detail)
            else:
                try:
                    hits = client.search("PyBrowser connection test", 1)
                    result = (True, f"{client.label} answered ({len(hits)} result).")
                except websearch.SearchError as exc:
                    result = (False, exc.message)
            self._post(lambda: done(*result))

        threading.Thread(target=work, name="team-test-search", daemon=True).start()

    def pull_sandbox_image(self, done: Callable[[bool, str], None]) -> None:
        image = self._sandbox_params()[1]

        def work() -> None:
            ok, message = sandbox_mod.pull_image(image)
            self._post(lambda: (self.recheck_sandbox(), done(ok, message)))

        threading.Thread(target=work, name="team-pull-image", daemon=True).start()

    def _post(self, fn: Callable[[], None]) -> None:
        try:
            self._dispatcher.post(fn)
        except GuiDispatchShutdown:
            pass

    # -- generated files -> the existing Downloads system ------------------------------
    def _download_directory(self) -> str:
        if self._downloads_dir:
            return self._downloads_dir
        from app.config import downloads_path

        return str(downloads_path())

    def save_to_downloads(self, file_name: str, text: str, mission_id: int = 0, *,
                          subfolder: str = "", directory: str | None = None, overwrite: bool = False) -> str:
        """Write a generated file via DownloadManager (so it appears in the
        Downloads window). Returns the full path. Falls back to a plain write
        only when the window has no download manager (tests, embedding)."""
        origin = f"pybrowser://ai-team/{mission_id}"
        target_dir = directory or self._download_directory()
        if self._downloads is not None:
            item = self._downloads.save_generated(file_name, text, target_dir, origin, subfolder=subfolder,
                                                  overwrite=overwrite)
            path = os.path.join(item.directory, item.file_name)
        else:
            from app.browser.downloads import DownloadManager

            item = DownloadManager.save_generated(_Standalone(), file_name, text, target_dir, origin,
                                                  subfolder=subfolder, overwrite=overwrite)
            path = os.path.join(item.directory, item.file_name)
        self.file_saved.emit(f"Saved {item.file_name} to {item.directory} \u2014 see Downloads (Ctrl+J)")
        return path

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


class _Standalone:
    """Just enough of DownloadManager for save_generated when no window owns one."""

    def __init__(self) -> None:
        self._items: dict = {}
        self._next_id = 1

        class _Sig:
            def emit(self, *_a) -> None:
                pass
        self.started = _Sig()
        self.finished = _Sig()


def websearch_safe(message: str, secret: str) -> str:
    from app.team.llm import redact

    return redact(message, secret)
