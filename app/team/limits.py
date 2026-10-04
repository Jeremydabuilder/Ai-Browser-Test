"""Execution limits for a Team run - all configurable, all bounded.

Defaults suit Groq's free-tier rate limits: two requests in flight, a hard
cap on total model calls, and exponential backoff that honours Retry-After.
Settings keys are ``team_<field>``; values outside the hard bounds are
clamped, never trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class TeamLimits:
    max_concurrency: int = 2
    max_tasks: int = 10
    max_model_calls: int = 60
    max_revision_rounds: int = 2
    #: Retries of one model call after a rate limit / transient error.
    max_retries: int = 3
    max_backoff_s: float = 30.0
    #: Wall-clock cap on one task (all of its model calls and checks).
    task_timeout_s: float = 240.0
    #: One re-run of a task whose output could not be parsed.
    task_retries: int = 1
    #: Characters of upstream artifacts + sources handed to one model call.
    max_context_chars: int = 24000
    max_output_tokens: int = 4096
    # Sandbox (Tester)
    check_timeout_s: float = 60.0
    max_checks: int = 4
    sandbox_memory_mb: int = 1024
    sandbox_cpu_s: int = 60
    # Reading search-result pages in full
    max_fetch_pages: int = 3
    fetch_timeout_s: float = 10.0
    fetch_max_chars: int = 20000
    # Task-level recovery after a rate limit (on top of per-call retries)
    rate_limit_requeues: int = 2
    rate_limit_cooldown_s: float = 20.0

    #: field -> (low, high). The only place bounds live.
    BOUNDS = {
        "max_concurrency": (1, 4), "max_tasks": (1, 20), "max_model_calls": (4, 300),
        "max_revision_rounds": (0, 4), "max_retries": (0, 8), "max_backoff_s": (1.0, 120.0),
        "task_timeout_s": (1.0, 1800.0), "task_retries": (0, 3),
        "max_context_chars": (2000, 120000), "max_output_tokens": (256, 16000),
        "check_timeout_s": (5.0, 600.0), "max_checks": (1, 10),
        "sandbox_memory_mb": (128, 8192), "sandbox_cpu_s": (5, 600),
        "max_fetch_pages": (0, 6), "fetch_timeout_s": (3.0, 60.0), "fetch_max_chars": (2000, 60000),
        "rate_limit_requeues": (0, 5), "rate_limit_cooldown_s": (1.0, 300.0),
    }

    def clamped(self) -> "TeamLimits":
        values = {}
        for f in fields(self):
            value = getattr(self, f.name)
            low, high = self.BOUNDS[f.name]
            values[f.name] = type(f.default)(max(low, min(high, value)))
        return TeamLimits(**values)

    @classmethod
    def from_settings(cls, settings) -> "TeamLimits":
        """Read ``team_<field>`` overrides; anything unparseable is ignored."""
        base = cls()
        values = {}
        for f in fields(cls):
            raw = None
            try:
                raw = settings.get(f"team_{f.name}", "") if settings is not None else ""
            except Exception:  # noqa: BLE001 - preferences are never load-bearing
                raw = ""
            if raw not in ("", None):
                try:
                    values[f.name] = type(f.default)(float(raw))
                except (TypeError, ValueError):
                    pass
        return cls(**{**{f.name: getattr(base, f.name) for f in fields(cls)}, **values}).clamped()
