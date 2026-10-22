"""Qt glue for AI Mode: owns the conversation, runs questions off the GUI thread, publishes page state.

The window connects ``state_changed`` to every open AI Mode tab (``PB.setState``). Credentials come from
the same place as the rest of the AI features (``resolve_provider``: OS keyring entry saved by "Configure AI
Agent", else the provider's environment variable) - AI Mode adds no key storage of its own.
"""

from __future__ import annotations

import json
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal

from app.aimode.engine import AiModeSession
from app.gui_dispatch import GuiDispatcher, GuiDispatchShutdown
from app.team import websearch
from app.team.limits import TeamLimits
from app.team.llm import CancelToken, make_client_factory, resolve_provider
from app.team.webfetch import PageFetcher


class AiModeController(QObject):
    #: The page state as JSON, ready for ``PB.setState``. Always emitted on the GUI thread.
    state_changed = Signal(str)

    def __init__(self, settings, parent: QObject | None = None, *, client_factory: Callable | None = None,
                 search_transport=None, page_fetcher=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._client_factory = client_factory
        self._search_transport = search_transport
        self._page_fetcher = page_fetcher
        self._dispatcher = GuiDispatcher(parent=self)
        self._session: AiModeSession | None = None
        self._status = ""
        self._busy = False
        self._token: CancelToken | None = None
        self._provider = None
        self._closed = False

    # -- environment (cached: reading the keyring on every page render would be wasteful) -------------
    def refresh_environment(self) -> None:
        self._provider = None
        self._session = None
        self.state_changed.emit(self.state_json())

    def _status_of_provider(self):
        if self._provider is None:
            self._provider = resolve_provider(self._settings)
        return self._provider

    def _search_status(self):
        return websearch.resolve_search(self._settings)

    def _limits(self) -> TeamLimits:
        return TeamLimits.from_settings(self._settings)

    def _session_for(self) -> AiModeSession:
        if self._session is None:
            status = self._status_of_provider()
            factory = self._client_factory or make_client_factory(status, self._settings, self._limits())
            search = self._search_status()
            web = websearch.WebSearch(search, transport=self._search_transport) if search.available else None
            limits = self._limits()
            fetcher = None
            if web is not None and limits.max_fetch_pages > 0:
                fetcher = self._page_fetcher or PageFetcher(
                    timeout=limits.fetch_timeout_s, deadline=limits.fetch_timeout_s * 2, max_chars=limits.fetch_max_chars)
            self._session = AiModeSession(factory, web_search=web, fetcher=fetcher, limits=limits,
                                          secret=status.secret, label=status.label, on_status=self._on_status)
        return self._session

    # -- state ---------------------------------------------------------------------------------
    def state(self) -> dict:
        provider = self._client_factory is not None or self._status_of_provider().available
        search = self._search_status()
        return {"turns": self._session.public() if self._session is not None else [], "busy": self._busy,
                "status": self._status, "providerReady": provider, "searchReady": search.available,
                "searchLabel": search.label if search.available else ""}

    def state_json(self) -> str:
        return json.dumps(self.state(), ensure_ascii=False).replace("<", "\\u003c")

    def _publish(self) -> None:
        if not self._closed:
            self.state_changed.emit(self.state_json())

    def _on_status(self, text: str) -> None:           # worker thread
        self._status = text
        try:
            self._dispatcher.post(self._publish)
        except GuiDispatchShutdown:
            pass

    # -- actions -------------------------------------------------------------------------------
    def ask(self, question: str) -> bool:
        if self._busy or self._closed or not question.strip():
            return False
        if self._client_factory is None and not self._status_of_provider().available:
            self._publish()
            return False
        session = self._session_for()
        self._busy, self._status = True, "Starting\u2026"
        self._token = CancelToken()
        token = self._token
        turn = session.begin(question)          # shown at once as a pending turn
        self._publish()

        def work() -> None:
            try:
                session.ask(question, token, turn)
            finally:
                try:
                    self._dispatcher.post(self._finished)
                except GuiDispatchShutdown:
                    pass

        threading.Thread(target=work, name="aimode-ask", daemon=True).start()
        return True

    def _finished(self) -> None:
        self._busy, self._status, self._token = False, "", None
        self._publish()

    def stop(self) -> None:
        if self._token is not None:
            self._token.cancel()

    def new_chat(self) -> None:
        self.stop()
        if self._session is not None and not self._busy:
            self._session.reset()
        self._publish()

    def shutdown(self) -> None:
        self._closed = True
        self.stop()
