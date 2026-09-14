"""TaskRunner: the timer that fires a ScheduledTask - not a second Mission
execution engine. Firing a due task means exactly two calls any interactive
message already makes: MissionService.start()/resume() (so the run is
attributed to a Mission, the same as a typed message that looks like a
task), then AgentSession.send(goal). Everything after that - tool calls,
approvals, steps - runs through the one AgentSession this window already
owns.

Because there is exactly one AgentSession per window, there is exactly one
execution slot: a scheduled run and an interactive one cannot happen at the
same time. This runner defers to whichever is already using it (see
_tick) rather than queuing a second one - the same rule Skills already
follow (MainWindow._run_skill refuses while session.busy).

No OS-level daemon: this QTimer only fires while the app process is running
and this window is open. See app/missions/scheduler.py for the schedule
arithmetic and app/storage/scheduled_tasks.py for persistence.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from app.agent.session import AgentState, StepState
from app.agent.tools import READ_ONLY_TOOLS, SEARCH_TOOLS
from app.missions.scheduler import ScheduleKind, ScheduledTask, TaskState, compute_next_run, utc_now
from app.storage.scheduled_tasks import ScheduledTaskStore

#: How often to check for due tasks. Coarse on purpose: nothing in this
#: phase promises second-accurate firing, only "the app must be open" -
#: see the module docstring on schedules and daemons.
_POLL_INTERVAL_MS = 15_000


class TaskRunner(QObject):
    #: A scheduled run completed successfully. Carries the ScheduledTask.
    mission_completed = Signal(object)
    #: A scheduled run ended in failure. Carries the ScheduledTask.
    mission_failed = Signal(object)
    #: A scheduled run is waiting on the user to approve a sensitive action.
    approval_required = Signal(object)

    def __init__(
        self,
        store: ScheduledTaskStore,
        missions,
        session_provider: Callable[[], object | None],
        parent: QObject | None = None,
        workspace_switcher: Callable[[str], None] | None = None,
        ownership: "Any | None" = None,
        device_id: str = "",
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._missions = missions
        #: Phase 20 Part 11 - an OwnershipStore (app.storage.sync_store),
        #: or None (the default, unchanged pre-sync behavior: every task
        #: runs on whichever device polls it, since there is only ever one
        #: device before sync exists). When given, a task synced from/to
        #: another device and not owned by this one is skipped here -
        #: never run twice just because its definition now exists on two
        #: machines.
        self._ownership = ownership
        self._device_id = device_id
        #: Returns the window's single AgentSession, building it if this is
        #: the first use - or None if the agent has no working credential.
        #: Never builds a second session: this is the exact same accessor
        #: interactive use goes through.
        self._session_provider = session_provider
        #: Phase 17: switches the window's current workspace (tabs, Mission
        #: scope, MCP visibility, isolated profile) before a bound task
        #: fires - see app/workspaces/. A background timer firing must never
        #: silently run in whatever workspace happens to be active; unlike
        #: a recorded workflow (MainWindow._confirm_workflow_workspace) an
        #: unattended task cannot block on a dialog, so it switches rather
        #: than asking. None (e.g. in tests) means "don't switch."
        self._workspace_switcher = workspace_switcher
        self._current_task_id: int | None = None
        self._current_run_id: int | None = None
        self._current_start: float = 0.0
        self._pending_error: str | None = None
        self._session = None

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        #: Public so other schedule-shaped features (Page Watches - see
        #: app/watches/runner.py) can piggyback their own periodic check on
        #: this same timer instead of starting a second background timing
        #: system, per that phase's own architecture rule.
        self.timer = self._timer

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    # -- crash recovery -----------------------------------------------------
    def recover_after_restart(self) -> list[ScheduledTask]:
        """Called once at startup, before the timer starts. Any task still
        marked "running" means the app died mid-run - there is no live
        AgentSession left to finish it, and never was one, since this is a
        fresh process. Recovery never resumes such a task automatically: it
        always moves to failed, with a message that tells the user whether a
        write might have happened, so the decision to retry is always
        theirs. Returns the recovered tasks, so the caller can tell the user.
        """
        recovered = []
        now = utc_now().isoformat()
        for task in self._store.running_tasks():
            if task.write_attempted:
                message = ("Interrupted while a write action may have been in progress - "
                          "review before retrying.")
            else:
                message = "Interrupted before any write actions - safe to retry."
            self._store.record_run_result(
                task.id, state=TaskState.FAILED, next_run_at=None,
                last_run_at=task.last_run_at or now, duration_s=0.0, error=message)
            open_run = self._open_run_for(task.id)
            if open_run is not None:
                self._store.record_run_finish(
                    open_run["id"], finished_at=now, outcome="failed", error=message)
            recovered.append(task)
        return recovered

    def _open_run_for(self, task_id: int) -> dict | None:
        for run in self._store.runs_for_task(task_id, limit=5):
            if run["finished_at"] is None:
                return run
        return None

    # -- the poll loop --------------------------------------------------------
    def _tick(self) -> None:
        if self._current_task_id is not None:
            return          # a scheduled run is already in flight
        session = self._session_provider()
        if session is None or session.busy:
            return          # the one execution slot is busy, or there is no agent
        due = self._store.due_tasks(utc_now())
        if self._ownership is not None:
            due = [t for t in due
                  if self._ownership.is_owned_by("scheduled_task", str(t.id), self._device_id)]
        if not due:
            return
        task = due[0]
        offline_reason = self._local_endpoint_offline_reason(session)
        if offline_reason is not None:
            # Part 18: never silently fall back to a different provider -
            # this session's transport is whatever config.provider says,
            # always, and nothing here ever substitutes a cloud one. Marked
            # failed with the reason visible in Task Center, and the
            # schedule still advances normally (the same recurrence math
            # _on_finished uses for an ordinary failed run) so a daily/
            # weekly task tries again next time rather than spinning on
            # every 15s poll while the local service stays down.
            self._fail_without_running(task, offline_reason)
            return
        self._fire(task, session)

    def _local_endpoint_offline_reason(self, session) -> str | None:
        """None if this task may run; otherwise the exact reason it can't.

        Only ever checks reachability, never tool/vision capability - a
        capability probe is a real extra request best reserved for an
        explicit user action (Tools -> Configure AI Agent), not something
        that runs on an unattended 15s poll.
        """
        from app.agent.config import is_local_provider

        config = getattr(session, "config", None)
        if config is None or not is_local_provider(config.provider):
            return None
        from app.agent.config import PROVIDER_LM_STUDIO, PROVIDER_LOCAL_OPENAI, PROVIDER_OLLAMA
        from app.agent.local_providers import (
            LMStudioClient, LocalOpenAICompatibleClient, OllamaClient,
        )

        client_class = {PROVIDER_OLLAMA: OllamaClient, PROVIDER_LM_STUDIO: LMStudioClient,
                        PROVIDER_LOCAL_OPENAI: LocalOpenAICompatibleClient}.get(config.provider)
        if client_class is None:
            return None
        ok, message = client_class.test_connection(
            "", "", base_url=(config.local_endpoint or "").strip())
        # A missing/blank model is expected here (this is a reachability
        # check, not a real chat request) and must never itself count as
        # "offline" - only a genuine connection failure does.
        if ok or "choose" in message.lower():
            return None
        return message

    def _fail_without_running(self, task: ScheduledTask, reason: str) -> None:
        now = utc_now()
        next_run_at = None
        if task.schedule_kind != ScheduleKind.ONCE:
            next_run_at_dt = compute_next_run(
                task.schedule_kind, now=now, schedule_at=task.schedule_at,
                time_of_day=task.time_of_day, weekday=task.weekday,
                interval_seconds=task.interval_seconds, last_run_at=now)
            next_run_at = next_run_at_dt.isoformat() if next_run_at_dt else None
        state = TaskState.FAILED if task.schedule_kind == ScheduleKind.ONCE else TaskState.QUEUED
        self._store.record_run_result(
            task.id, state=state, next_run_at=next_run_at,
            last_run_at=now.isoformat(), duration_s=0.0, error=reason)
        updated = self._store.get(task.id)
        if updated is not None:
            self.mission_failed.emit(updated)

    def run_now(self, task_id: int) -> bool:
        """Force a task to run immediately, ignoring its next_run_at -
        refused if a scheduled run is already in flight or the slot is busy,
        exactly like waiting for the timer would be."""
        if self._current_task_id is not None:
            return False
        session = self._session_provider()
        if session is None or session.busy:
            return False
        task = self._store.get(task_id)
        if task is None or task.state not in (TaskState.QUEUED, TaskState.PAUSED):
            return False
        self._fire(task, session)
        return True

    def pause(self, task_id: int) -> None:
        task = self._store.get(task_id)
        if task is not None and task.state == TaskState.QUEUED:
            self._store.set_state(task_id, TaskState.PAUSED)

    def resume(self, task_id: int) -> None:
        task = self._store.get(task_id)
        if task is None or task.state != TaskState.PAUSED:
            return
        next_run_at = compute_next_run(
            task.schedule_kind, now=utc_now(), schedule_at=task.schedule_at,
            time_of_day=task.time_of_day, weekday=task.weekday,
            interval_seconds=task.interval_seconds)
        self._store.record_run_result(
            task_id, state=TaskState.QUEUED,
            next_run_at=next_run_at.isoformat() if next_run_at else None,
            last_run_at=task.last_run_at or "", duration_s=task.last_duration_s or 0.0,
            error=task.last_error)

    # -- firing one task ------------------------------------------------------
    def _fire(self, task: ScheduledTask, session) -> None:
        if task.workspace_id and self._workspace_switcher is not None:
            self._workspace_switcher(task.workspace_id)
        self._current_task_id = task.id
        self._current_start = time.monotonic()
        self._pending_error = None
        self._session = session
        self._store.set_state(task.id, TaskState.RUNNING)
        started_at = utc_now().isoformat()
        self._current_run_id = self._store.record_run_start(task.id, started_at)

        if task.mission_id is not None:
            self._missions.resume(task.mission_id)
        else:
            mission = self._missions.start(task.goal)
            if mission is not None:
                self._store.set_mission_id(task.id, mission.id, mission.title)

        self._connect(session)
        if not session.send(task.goal):
            # busy is already checked above, but stay honest if it changed
            # between the check and here.
            self._disconnect(session)
            self._store.record_run_result(
                task.id, state=TaskState.QUEUED, next_run_at=task.next_run_at,
                last_run_at=task.last_run_at or "", duration_s=0.0, error=None)
            self._current_task_id = None
            self._current_run_id = None

    def _connect(self, session) -> None:
        session.step_changed.connect(self._on_step_changed)
        session.confirmation_required.connect(self._on_confirmation_required)
        session.state_changed.connect(self._on_state_changed)
        session.error.connect(self._on_error)
        session.finished.connect(self._on_finished)

    def _disconnect(self, session) -> None:
        for signal, slot in (
            (session.step_changed, self._on_step_changed),
            (session.confirmation_required, self._on_confirmation_required),
            (session.state_changed, self._on_state_changed),
            (session.error, self._on_error),
            (session.finished, self._on_finished),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_step_changed(self, step) -> None:
        if self._current_task_id is None:
            return
        if step.state == StepState.RUNNING and step.tool \
                and step.tool not in READ_ONLY_TOOLS and step.tool not in SEARCH_TOOLS:
            self._store.set_write_attempted(self._current_task_id, True)

    def _on_confirmation_required(self, request) -> None:
        if self._current_task_id is None:
            return
        self._store.set_state(self._current_task_id, TaskState.WAITING_FOR_APPROVAL)
        task = self._store.get(self._current_task_id)
        if task is not None:
            self.approval_required.emit(task)

    def _on_state_changed(self, state: str) -> None:
        if self._current_task_id is None:
            return
        # Coming back from AWAITING_CONFIRMATION - approved or declined -
        # means the run is active again, whichever way the user answered.
        if state in (AgentState.ACTING, AgentState.THINKING):
            task = self._store.get(self._current_task_id)
            if task is not None and task.state == TaskState.WAITING_FOR_APPROVAL:
                self._store.set_state(self._current_task_id, TaskState.RUNNING)

    def _on_error(self, message: str) -> None:
        if self._current_task_id is not None:
            self._pending_error = message

    def _on_finished(self) -> None:
        task_id = self._current_task_id
        run_id = self._current_run_id
        if task_id is None:
            return
        session = self._session
        duration_s = time.monotonic() - self._current_start
        error = self._pending_error
        now = utc_now()
        task = self._store.get(task_id)
        self._disconnect(session)
        self._current_task_id = None
        self._current_run_id = None
        self._pending_error = None
        self._session = None
        if task is None:
            return

        failed = error is not None
        # A "once" schedule does not repeat either way; a recurring one
        # keeps going even after a failed run - last_error stays visible in
        # the Task Center so the failure is never silent, but a single bad
        # run does not permanently kill a daily/weekly/interval schedule.
        if task.schedule_kind == ScheduleKind.ONCE:
            state = TaskState.FAILED if failed else TaskState.COMPLETED
            next_run_at = None
        else:
            state = TaskState.QUEUED
            next_run_at_dt = compute_next_run(
                task.schedule_kind, now=now, schedule_at=task.schedule_at,
                time_of_day=task.time_of_day, weekday=task.weekday,
                interval_seconds=task.interval_seconds, last_run_at=now)
            next_run_at = next_run_at_dt.isoformat() if next_run_at_dt else None

        self._store.record_run_result(
            task_id, state=state, next_run_at=next_run_at,
            last_run_at=now.isoformat(), duration_s=duration_s, error=error)
        if run_id is not None:
            self._store.record_run_finish(
                run_id, finished_at=now.isoformat(),
                outcome="failed" if failed else "completed", error=error)

        updated = self._store.get(task_id)
        if updated is None:
            return
        if failed:
            self.mission_failed.emit(updated)
        else:
            self.mission_completed.emit(updated)
