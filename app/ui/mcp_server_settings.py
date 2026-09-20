"""Settings -> External AI Access: the PyBrowser MCP Server (Phase 11),
extended into a client-oriented setup page (Phase 12).

"Connect an AI" shows one card per supported client (ChatGPT, Claude,
Cursor, VS Code, Custom MCP Client). Every card's "Set up" flow - preset
permissions, pairing, config generation, verification - goes through the
exact same app/mcp_server/auth.py, client_configs.py and verification.py
as any other client; nothing here is client-specific except which text
and generator function a card points at. The paired-client table below
stays as the advanced, all-clients-at-once view from Phase 11.
"""

from __future__ import annotations

import os
import sys
import threading
import weakref

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.mcp_server import auth, client_configs, permissions
from app.mcp_server.types import Capability, ClientType, VerificationStatus
from app.mcp_server.verification import verify_connection
from app.ui import theme

#: Which generator, default connection method, and starting preset each
#: client card uses. This is the ONLY place client identity changes
#: behavior - purely which text/config comes out, never auth or scope.
_CLIENT_SETUP: dict[ClientType, dict] = {
    ClientType.CURSOR: {
        "generator": lambda url, token: client_configs.cursor_config(url, token),
        "connection_method": "direct_http", "default_preset": "research",
    },
    ClientType.VSCODE: {
        "generator": lambda url, token: client_configs.vscode_config(url, token),
        "connection_method": "direct_http", "default_preset": "research",
    },
    ClientType.CLAUDE: {
        "generator": lambda url, token: client_configs.claude_desktop_bridge_config(url, token),
        "connection_method": "stdio_bridge", "default_preset": "read_only",
    },
    ClientType.CHATGPT: {
        "generator": lambda url, _token: client_configs.chatgpt_connector_info(url),
        "connection_method": "remote_tunnel", "default_preset": "read_only",
    },
    ClientType.GENERIC: {
        "generator": None,  # built from live capabilities - see _generate
        "connection_method": "direct_http", "default_preset": "read_only",
    },
}

_PRESET_ORDER = ["read_only", "research", "mission_assistant", "full_access"]

# Temporary diagnostic instrumentation for the intermittent macOS crash in
# ClientSetupDialogTests.test_verifying_a_real_pairing_reaches_verified.
# Off by default (PYBROWSER_VERIFY_DIAG unset) - a normal test/production
# run never touches this. Remove once the crash is root-caused.
_VERIFY_DIAG = os.environ.get("PYBROWSER_VERIFY_DIAG") == "1"


def _diag(msg: str) -> None:
    if _VERIFY_DIAG:
        t = threading.current_thread()
        print(f"VERIFYDIAG [{t.name}/{t.ident}] {msg}", file=sys.stderr, flush=True)


#: Verify threads (and their worker _Verifier) that are still running or
#: pending when nothing else references them - the dialog that started
#: them was closed, or destroyed by its parent, before the round trip
#: finished (see ClientSetupDialog.done()). Both sets exist for the same
#: reason: a QObject with no Qt parent whose only Python reference is
#: dropped while it is still in use is either "QThread: Destroyed while
#: thread is still running" (the thread, if dropped while running) or a
#: worker deleted out from under the thread that is about to call a
#: method on it (the verifier, if dropped before/during its own run() -
#: confirmed directly: an earlier version of this code cleared
#: ClientSetupDialog._verifier immediately in the abandoned-dialog case,
#: and the worker thread's queued invocation of verifier.run() silently
#: never happened at all). These sets are that reference, kept until each
#: object's own `finished`/deletion proves it is safe to let go.
#: One-directional (these sets hold a QThread/_Verifier; nothing in them
#: points back to a dialog), so they can never be part of a reference
#: cycle.
_IN_FLIGHT_VERIFY_THREADS: set[QThread] = set()
_IN_FLIGHT_VERIFIERS: set["_Verifier"] = set()


class _Verifier(QObject):
    """Runs verify_connection() on a background thread. Verification
    speaks real HTTP to this same PyBrowser MCP server, and a safe tool
    call routes through the GUI-thread bridge on the SERVER side - so this
    must never run on the GUI thread itself, or the two block each other."""

    done = Signal(object)

    def __init__(self, url: str, token: str) -> None:
        super().__init__()
        self._url = url
        self._token = token
        _diag(f"_Verifier.__init__ id={id(self)} thread={self.thread()}")

    def run(self) -> None:
        _diag(f"_Verifier.run start id={id(self)} qthread={QThread.currentThread()} "
              f"affinity={self.thread()}")
        result = verify_connection(self._url, self._token)
        _diag(f"_Verifier.run got result id={id(self)} status={result.status!r} - emitting done")
        self.done.emit(result)
        _diag(f"_Verifier.run done.emit returned id={id(self)}")
        # Request our own deletion from right here, inside run() - i.e.
        # still on our own thread's stack, at the same loop level the
        # `started` signal invoked us at. deleteLater() posts a
        # QEvent::DeferredDelete that Qt only delivers to a loop at or
        # below the level it was posted from; calling it later from the
        # GUI-thread slot that "done" is queued-connected to is a *cross*
        # thread postEvent racing against that same slot's thread.quit(),
        # and there is no guarantee the deferred-delete gets flushed
        # before the worker's exec() loop actually exits (this raced and
        # lost - reliably, 100% of the time - on real Windows/macOS Qt
        # event dispatchers, though never locally on Linux/glib). Posting
        # it here removes the race: it is queued before this method even
        # returns, well before anything asks the loop to quit.
        self.deleteLater()
        _diag(f"_Verifier.run deleteLater() posted id={id(self)}, run() returning")


class _VerifyReceiver(QObject):
    """Lives on the GUI thread purely so Qt's automatic queued-connection
    detection correctly marshals _Verifier.done's cross-thread emission
    here rather than running the connected call on the worker thread -
    a bound method of a plain (non-QObject) callable, or a
    functools.partial, has no thread affinity Qt can detect and would run
    wherever the signal happens to be emitted from (the worker thread),
    which is exactly the "Thread tried to wait on itself"-class hazard
    _on_verify's own bound-slot comment already documents for the
    thread.started->verifier.run connection.

    Deliberately NOT a bound method of ClientSetupDialog itself: this
    holds only a weakref to the dialog, so receiver<->dialog can never
    form the reference cycle dialog<->_Verifier used to (dialog held a
    strong `_verifier` attribute, and the direct `verifier.done.connect
    (self._on_verified)` connection held a bound method whose __self__
    was the dialog - a cycle only cyclic GC could break). With this
    indirection, ordinary refcounting reclaims every object in one verify
    round trip the moment nothing needs it, so there is nothing left for
    cyclic GC to ever have to collect from this code path - which is what
    lets the gc.collect() this class replaces be removed outright rather
    than merely relocated.
    """

    def __init__(self, dialog: "ClientSetupDialog") -> None:
        super().__init__()
        self._dialog_ref = weakref.ref(dialog)

    def deliver(self, result: object) -> None:
        dialog = self._dialog_ref()
        if dialog is not None:
            dialog._on_verified(result)


class ClientSetupDialog(QDialog):
    """Preset permissions -> pair -> generated config -> verify. One
    dialog shape shared by every client card; ``client_type`` only picks
    the label, default preset, and config generator."""

    def __init__(self, client_type: ClientType, server, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client_type = client_type
        self._server = server
        self._client_id: str | None = None
        self._token: str | None = None
        self._thread: QThread | None = None
        self._verifier: _Verifier | None = None
        self._verify_receiver: _VerifyReceiver | None = None
        setup = _CLIENT_SETUP[client_type]
        self.setWindowTitle(f"Connect {ClientType.labels()[client_type]}")
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        form = QFormLayout()
        self.name_edit = QLineEdit(ClientType.labels()[client_type], self)
        form.addRow("Name:", self.name_edit)

        self.preset_combo = QComboBox(self)
        for preset in _PRESET_ORDER:
            self.preset_combo.addItem(permissions.PERMISSION_PRESET_LABELS[preset], preset)
        self.preset_combo.setCurrentIndex(_PRESET_ORDER.index(setup["default_preset"]))
        form.addRow("Permissions:", self.preset_combo)
        layout.addLayout(form)

        self._checks: dict[Capability, QCheckBox] = {}
        checks_box = QVBoxLayout()
        for capability, label in Capability.labels().items():
            box = QCheckBox(label, self)
            self._checks[capability] = box
            checks_box.addWidget(box)
        layout.addLayout(checks_box)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset_to_checks)
        self._apply_preset_to_checks()

        self.pair_button = QPushButton("Generate && Pair", self)
        self.pair_button.clicked.connect(self._on_pair)
        layout.addWidget(self.pair_button)

        self.tunnel_warning = QLabel(self)
        self.tunnel_warning.setWordWrap(True)
        self.tunnel_warning.hide()
        layout.addWidget(self.tunnel_warning)

        self.instructions_label = QLabel(self)
        self.instructions_label.setWordWrap(True)
        self.instructions_label.hide()
        layout.addWidget(self.instructions_label)

        self.config_text = QPlainTextEdit(self)
        self.config_text.setReadOnly(True)
        self.config_text.setMaximumHeight(140)
        self.config_text.hide()
        layout.addWidget(self.config_text)

        action_row = QHBoxLayout()
        self.copy_button = QPushButton("Copy Config", self)
        self.copy_button.clicked.connect(self._on_copy)
        self.copy_button.hide()
        action_row.addWidget(self.copy_button)
        self.verify_button = QPushButton("Verify Connection", self)
        self.verify_button.clicked.connect(self._on_verify)
        self.verify_button.setEnabled(False)
        action_row.addWidget(self.verify_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.status_label = QLabel("Not configured", self)
        layout.addWidget(self.status_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

    def _apply_preset_to_checks(self) -> None:
        preset = self.preset_combo.currentData()
        selected = set(permissions.preset_capabilities(preset))
        for capability, box in self._checks.items():
            box.setChecked(capability.value in selected)

    def _on_pair(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Name required", "Give this client a name first.")
            return
        capabilities = [c.value for c, box in self._checks.items() if box.isChecked()]
        setup = _CLIENT_SETUP[self._client_type]
        client, token = auth.pair_client(
            self._server.store, display_name=name, capabilities=capabilities,
            client_type=self._client_type.value, connection_method=setup["connection_method"])
        self._client_id = client.id
        self._token = token

        url = client_configs.mcp_url(self._server.host, self._server.port)
        if setup["generator"] is not None:
            generated = setup["generator"](url, token)
        else:
            generated = client_configs.generic_client_info(url, token, capabilities)

        self.instructions_label.setText(generated.instructions)
        self.instructions_label.show()
        if generated.config_text:
            self.config_text.setPlainText(generated.config_text)
            self.config_text.show()
            self.copy_button.show()
        if generated.requires_tunnel:
            self.tunnel_warning.setText(
                "Requires a tunnel: this client cannot reach a localhost server directly.")
            self.tunnel_warning.show()
            self._server.store.record_verification(
                client.id, VerificationStatus.REQUIRES_TUNNEL.value)
            self.status_label.setText(VerificationStatus.labels()[VerificationStatus.REQUIRES_TUNNEL])
        else:
            self._server.store.record_verification(
                client.id, VerificationStatus.CONFIG_GENERATED.value)
            self.status_label.setText(VerificationStatus.labels()[VerificationStatus.CONFIG_GENERATED])
            self.verify_button.setEnabled(True)

        self.name_edit.setEnabled(False)
        self.preset_combo.setEnabled(False)
        for box in self._checks.values():
            box.setEnabled(False)
        self.pair_button.setEnabled(False)

    def _on_copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.config_text.toPlainText())

    def _on_verify(self) -> None:
        if self._client_id is None or self._token is None:
            return
        if self._thread is not None:
            # A verify round trip is already in flight for this dialog -
            # never start a second one concurrently (would silently
            # abandon this dialog's handle on the first one, and could
            # eventually invoke _on_verified twice). The button is
            # disabled for exactly this duration in ordinary UI use; this
            # guard is what makes a second, programmatic _on_verify() call
            # (see the repeated-verify-calls test) a safe no-op instead.
            return
        url = client_configs.mcp_url(self._server.host, self._server.port)
        self.verify_button.setEnabled(False)
        self.status_label.setText("Verifying…")

        # Deterministic verification lifecycle (see _VerifyReceiver's
        # docstring for why gc.collect() is no longer needed here at all -
        # this used to force-collect a self<->_verifier reference cycle,
        # and is now built so that cycle never exists in the first place):
        #   1. create the QThread (no Qt parent - see below)
        #   2. create the worker (_Verifier)
        #   3. create a GUI-thread receiver holding only a weakref to us
        #   4. move the worker to the thread
        #   5. connect thread.started -> worker.run
        #   6. connect worker.done -> receiver.deliver (queued: receiver
        #      lives on the GUI thread, so Qt auto-marshals correctly)
        #   7. connect thread.finished -> thread.deleteLater (Qt-standard
        #      "delete a QThread once it actually stops" idiom)
        #   8. track the thread so something keeps it alive independent of
        #      this dialog (see _IN_FLIGHT_VERIFY_THREADS) - closing this
        #      dialog, or the app quitting, must never drop the last
        #      reference to a still-running QThread (that is exactly
        #      "QThread: Destroyed while thread is still running")
        #   9. start the thread
        # Deliberately no Qt parent on the QThread: a parented QThread
        # would be destroyed along with this dialog (its parent panel can
        # do that at any time - ClientSetupDialog is never explicitly
        # deleteLater()'d by its caller), which is unsafe while it is
        # still running. Lifetime is instead fully explicit via
        # _IN_FLIGHT_VERIFY_THREADS + its own finished->deleteLater.
        thread = QThread()
        verifier = _Verifier(url, self._token)
        receiver = _VerifyReceiver(self)
        verifier.moveToThread(thread)
        _diag(f"_on_verify moved verifier id={id(verifier)} to thread id={id(thread)}, "
              f"new affinity={verifier.thread()}")
        thread.started.connect(verifier.run)
        # Two independent consequences of the SAME terminal signal, not one
        # depending on the other: the thread must stop once verification
        # has a result regardless of whether this dialog is even still
        # around to receive it (receiver.deliver's weakref can and does go
        # dead - see done()/_teardown_verify - in which case nothing else
        # would ever call thread.quit(), leaving its exec() loop running
        # forever). _on_verified's own thread.quit()/wait() (normal path)
        # becomes a harmless, already-satisfied no-op once this has run.
        verifier.done.connect(thread.quit)
        verifier.done.connect(receiver.deliver)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: _IN_FLIGHT_VERIFY_THREADS.discard(t))
        # thread.finished only fires once run() (and thus verifier's own
        # role) has fully completed - safe to stop tracking the verifier
        # at the same point the thread's own tracking ends.
        thread.finished.connect(lambda v=verifier: _IN_FLIGHT_VERIFIERS.discard(v))
        _IN_FLIGHT_VERIFY_THREADS.add(thread)
        _IN_FLIGHT_VERIFIERS.add(verifier)

        self._thread = thread
        self._verifier = verifier
        self._verify_receiver = receiver
        _diag(f"_on_verify starting QThread id={id(thread)}")
        thread.start()
        _diag(f"_on_verify QThread.start() returned id={id(thread)} isRunning={thread.isRunning()}")

    def _on_verified(self, result) -> None:
        _diag(f"_on_verified entered on qthread={QThread.currentThread()} status={result.status!r}")
        self._teardown_verify(wait_for_thread=True)
        if self._client_id is not None:
            self._server.store.record_verification(self._client_id, result.status.value)
        self.status_label.setText(
            f"{VerificationStatus.labels().get(result.status, result.status.value)}"
            + (f" - {result.detail}" if result.detail else ""))
        self.verify_button.setEnabled(True)
        _diag("_on_verified returning")

    def _teardown_verify(self, *, wait_for_thread: bool) -> None:
        """Drop this dialog's own references to the in-flight verify
        round trip. Idempotent and safe to call whether or not a verify is
        actually running.

        ``wait_for_thread=True`` (the normal _on_verified path): the
        worker has already emitted its terminal result and requested its
        own deletion (see _Verifier.run) by the time this runs, so the
        thread's event loop is expected to stop immediately - quit()+
        wait() here is a short, bounded, deterministic join, not a
        network-timeout-length wait.

        ``wait_for_thread=False`` (the dialog is closing/being destroyed
        while verification is still running - see done()): never block
        the GUI thread waiting on a still-in-flight network call. The
        thread keeps running under _IN_FLIGHT_VERIFY_THREADS' own
        reference and cleans itself up via finished->deleteLater once
        verify_connection() actually returns - which needs verifier.done
        connected to thread.quit to STAY connected even here (see below);
        only the delivery-to-this-dialog connection is cut, so no
        callback ever reaches a dialog that has closed.
        """
        if self._verifier is not None and self._verify_receiver is not None:
            try:
                # Only this connection - never verifier.done -> thread.quit
                # (see _on_verify): a bare disconnect() with no arguments
                # would drop BOTH, and then nothing would ever tell the
                # worker thread to stop once it finishes, in exactly the
                # "dialog closed mid-verify" case this branch exists for -
                # an abandoned QThread spinning in its event loop forever.
                self._verifier.done.disconnect(self._verify_receiver.deliver)
            except (RuntimeError, TypeError):
                pass
        # The verifier schedules its own deleteLater() from inside run(),
        # on its own thread, before any result can be delivered (see
        # _Verifier.run) - so by construction that request was already
        # posted to the worker thread's queue. Just drop our reference;
        # don't delete it a second time.
        self._verifier = None
        self._verify_receiver = None
        thread = self._thread
        self._thread = None
        if thread is not None and wait_for_thread:
            _diag(f"_teardown_verify calling thread.quit() id={id(thread)} "
                  f"isRunning={thread.isRunning()}")
            thread.quit()
            waited_ok = thread.wait(2000)
            _diag(f"_teardown_verify thread.wait(2000) returned {waited_ok} "
                  f"isFinished={thread.isFinished()} id={id(thread)}")

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        # Covers every way this dialog can close (the Close button's
        # accept(), Esc/reject(), and the window's own X button, which
        # QDialog routes through reject() then done()): if a verify is
        # still running, drop only THIS dialog's references, without
        # blocking on or force-stopping the worker thread - see
        # _teardown_verify's docstring. The thread finishes and cleans
        # itself up independently; _verify_receiver already guards
        # against delivering a result to a dialog that is gone by then.
        self._teardown_verify(wait_for_thread=False)
        super().done(result)


class PairClientDialog(QDialog):
    """The original Phase 11 generic pairing flow - name + capability
    checkboxes, a token shown exactly once. Kept for direct use (e.g. from
    the advanced "Pair New AI Client…" button) alongside the newer
    per-client-type ClientSetupDialog above."""

    def __init__(self, store, host: str, port: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._host = host
        self._port = port
        self.setWindowTitle("Pair New AI Client")
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("e.g. ChatGPT, Claude Desktop")
        form.addRow("Name:", self.name_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("Permissions:", self))
        self._checks: dict[Capability, QCheckBox] = {}
        for capability, label in Capability.labels().items():
            box = QCheckBox(label, self)
            self._checks[capability] = box
            layout.addWidget(box)

        self._result_area = QPlainTextEdit(self)
        self._result_area.setReadOnly(True)
        self._result_area.setMaximumHeight(90)
        self._result_area.hide()
        layout.addWidget(self._result_area)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._generate_button = QPushButton("Generate", self)
        self._generate_button.clicked.connect(self._on_generate)
        buttons.addWidget(self._generate_button)
        self._close_button = QPushButton("Close", self)
        self._close_button.clicked.connect(self.accept)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

    def _on_generate(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Name required", "Give this client a name first.")
            return
        capabilities = [c.value for c, box in self._checks.items() if box.isChecked()]
        client, token = auth.pair_client(self._store, display_name=name, capabilities=capabilities)
        self._result_area.setPlainText(
            f"Pairing token (shown once - copy it now):\n{token}\n\n"
            f"Connection: http://{self._host}:{self._port}/mcp")
        self._result_area.show()
        self.name_edit.setEnabled(False)
        for box in self._checks.values():
            box.setEnabled(False)
        self._generate_button.setEnabled(False)


class _ClientCard(QFrame):
    """One row in "Connect an AI": type label, a one-line status, and a
    Set up/Manage button. The normal user should not need to understand
    JSON-RPC or bearer headers to read this - see Phase 12, Part 13."""

    def __init__(self, client_type: ClientType, panel: "ExternalAiAccessPanel",
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client_type = client_type
        self._panel = panel
        self.setFrameShape(QFrame.Shape.StyledPanel)
        m = theme.METRICS

        layout = QHBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_2, m.space_3, m.space_2)
        text_col = QVBoxLayout()
        self.title_label = QLabel(ClientType.labels()[client_type], self)
        self.title_label.setStyleSheet("font-weight:600;")
        text_col.addWidget(self.title_label)
        self.status_label = QLabel(self)
        text_col.addWidget(self.status_label)
        layout.addLayout(text_col, 1)
        self.action_button = QPushButton(self)
        self.action_button.clicked.connect(self._on_click)
        layout.addWidget(self.action_button)

    def refresh(self, clients: list) -> None:
        matching = [c for c in clients if c.client_type == self._client_type.value]
        active = next((c for c in matching if not c.revoked), None)
        if active is None:
            self.status_label.setText("Not configured")
            self.action_button.setText("Set up")
            return
        if active.last_verified_status == VerificationStatus.VERIFIED.value:
            label = "Verified"
        elif active.last_verified_status == VerificationStatus.REQUIRES_TUNNEL.value:
            label = "Requires tunnel"
        elif active.last_verified_status == VerificationStatus.AUTHENTICATION_FAILED.value:
            label = "Authentication failed"
        elif active.last_verified_status == VerificationStatus.UNREACHABLE.value:
            label = "Unreachable"
        elif active.capabilities:
            label = "Ready"
        else:
            label = "Config generated"
        capability_labels = [Capability.labels().get(Capability(c), c)
                             for c in active.capabilities]
        self.status_label.setText(f"{label} - {', '.join(capability_labels) or 'no permissions'}")
        self.action_button.setText("Manage")

    def _on_click(self) -> None:
        dialog = ClientSetupDialog(self._client_type, self._panel._server, self)
        dialog.exec()
        self._panel._refresh()


class ExternalAiAccessPanel(QWidget):
    """Status, Enable/Disable, "Connect an AI" client cards, and the
    advanced all-clients table with Revoke/View Activity."""

    _COLUMNS = ["Name", "Type", "Permissions", "Created", "Last used", "Status"]

    def __init__(self, mcp_server, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._server = mcp_server
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_4, m.space_5, m.space_4)
        layout.setSpacing(m.space_3)

        status_row = QHBoxLayout()
        self.status_label = QLabel(self)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        self.toggle_button = QPushButton(self)
        self.toggle_button.clicked.connect(self._on_toggle)
        status_row.addWidget(self.toggle_button)
        layout.addLayout(status_row)

        layout.addWidget(QLabel("Connect an AI", self))
        self._cards: dict[ClientType, _ClientCard] = {}
        for client_type in (ClientType.CURSOR, ClientType.CLAUDE, ClientType.CHATGPT,
                           ClientType.VSCODE, ClientType.GENERIC):
            card = _ClientCard(client_type, self)
            self._cards[client_type] = card
            layout.addWidget(card)

        layout.addWidget(QLabel("Advanced: all paired clients", self))
        self.table = QTableWidget(0, len(self._COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        button_row = QHBoxLayout()
        pair_button = QPushButton("Pair New AI Client…", self)
        pair_button.clicked.connect(self._on_pair)
        button_row.addWidget(pair_button)
        self.revoke_button = QPushButton("Revoke", self)
        self.revoke_button.clicked.connect(self._on_revoke)
        button_row.addWidget(self.revoke_button)
        self.activity_button = QPushButton("View Activity", self)
        self.activity_button.clicked.connect(self._on_view_activity)
        button_row.addWidget(self.activity_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._refresh()

    def _refresh(self) -> None:
        running = self._server.running
        self.status_label.setText(f"PyBrowser MCP Server: {'Running' if running else 'Stopped'}")
        self.toggle_button.setText("Disable" if running else "Enable")
        clients = self._server.store.list_clients()
        for card in self._cards.values():
            card.refresh(clients)
        self.table.setRowCount(len(clients))
        for row, client in enumerate(clients):
            labels = [Capability.labels().get(Capability(c), c) for c in client.capabilities]
            self.table.setItem(row, 0, QTableWidgetItem(client.display_name))
            self.table.setItem(row, 1, QTableWidgetItem(
                ClientType.labels().get(ClientType(client.client_type), client.client_type)))
            self.table.setItem(row, 2, QTableWidgetItem(", ".join(labels) or "(none)"))
            self.table.setItem(row, 3, QTableWidgetItem(client.created_at))
            self.table.setItem(row, 4, QTableWidgetItem(client.last_used_at or "Never"))
            status = "Revoked" if client.revoked else "Active"
            item = QTableWidgetItem(status)
            item.setData(Qt.ItemDataRole.UserRole, client.id)
            self.table.setItem(row, 5, item)

    def _selected_client_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 5)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_toggle(self) -> None:
        if self._server.running:
            self._server.stop()
        else:
            if not self._server.start():
                QMessageBox.warning(
                    self, "Could not start",
                    f"The MCP server could not bind to {self._server.host}:{self._server.port}.")
        self._refresh()

    def _on_pair(self) -> None:
        dialog = PairClientDialog(self._server.store, self._server.host, self._server.port, self)
        dialog.exec()
        self._refresh()

    def _on_revoke(self) -> None:
        client_id = self._selected_client_id()
        if client_id is None:
            return
        auth.revoke_client(self._server.store, client_id)
        self._refresh()

    def _on_view_activity(self) -> None:
        entries = self._server.store.recent_audit(limit=100)
        clients_by_id = {c.id: c for c in self._server.store.list_clients()}
        lines = []
        for e in entries:
            client = clients_by_id.get(e.client_id)
            name = client.display_name if client is not None else (e.client_id or "unknown")
            lines.append(f"{e.created_at}  {name}  {e.tool}  {e.outcome}  {e.duration_ms}ms")
        QMessageBox.information(
            self, "Recent Activity", "\n".join(lines) or "No activity recorded yet.")
