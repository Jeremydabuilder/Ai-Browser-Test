"""The Team's only door to a model provider.

Credentials are never handled here: ``make_client_factory`` resolves them
through the browser's existing mechanism (app.agent.credentials.resolve_for -
OS keyring, then the provider's environment variable) at the moment a run
starts, and the client object that holds the key never leaves this process.
Nothing here prints, logs, stores or renders the key; ``redact`` scrubs both
the exact key and anything shaped like one from every string the Team shows.

Rate limits are handled with *bounded* retries: at most ``max_retries``
re-attempts of one call, waiting the provider's own Retry-After when it sent
one (capped), else exponential backoff - and every wait is interruptible by
cancellation. Past the bound the call fails with a clear, typed error rather
than looping.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent.claude_client import ClaudeError
from app.team.limits import TeamLimits

#: Shapes of provider keys (Groq ``gsk_``, OpenAI ``sk-``, Anthropic
#: ``sk-ant-``, OpenRouter ``sk-or-``, Gemini ``AIza``) - scrubbed even if we
#: somehow never held that exact string.
_KEY_SHAPES = re.compile(r"(gsk_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{20,})")


class ErrorKind:
    NO_CREDENTIAL = "no_credential"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PROVIDER = "provider"
    BUDGET = "budget"
    BAD_OUTPUT = "bad_output"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    #: Failures that no retry or other task can recover from: stop the run.
    FATAL = (NO_CREDENTIAL, AUTH, QUOTA, BUDGET)


class TeamError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        #: Seconds the provider last asked us to wait (rate limits only).
        self.retry_after = retry_after


class Cancelled(Exception):
    """Raised inside a worker when the run (or its task) was cancelled."""


class CancelToken:
    """A cancellation flag with an interruptible wait; tokens chain, so
    cancelling the mission cancels every task token derived from it."""

    def __init__(self, parent: "CancelToken | None" = None) -> None:
        self._event = threading.Event()
        self._parent = parent

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (self._parent is not None and self._parent.cancelled)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True if cancelled before it elapsed."""
        step = 0.05
        waited = 0.0
        while waited < seconds:
            if self.cancelled:
                return True
            self._event.wait(min(step, seconds - waited))
            waited += step
        return self.cancelled


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


def redact(text: str, secret: str = "") -> str:
    if not text:
        return text
    if secret and len(secret) >= 6:
        text = text.replace(secret, "[redacted]")
    return _KEY_SHAPES.sub("[redacted]", text)


def classify_error(exc: ClaudeError) -> str:
    """Map a provider error (already phrased for a person) onto an ErrorKind.
    ``ClaudeError`` carries no status code, so the message is what we have."""
    message = (exc.message or "").lower()
    if "free quota is exhausted" in message or "credit" in message:
        return ErrorKind.QUOTA
    if "rate limit" in message:
        return ErrorKind.RATE_LIMIT
    if "rejected the api key" in message or "does not have permission" in message \
            or "no " in message and "api key is configured" in message:
        return ErrorKind.AUTH
    if "could not reach" in message or "took too long" in message:
        return ErrorKind.NETWORK
    return ErrorKind.PROVIDER


class TeamLLM:
    """Bounded, cancellable, budgeted model calls for the whole team."""

    def __init__(self, client_factory: Callable[[], Any], limits: TeamLimits, cancel: CancelToken,
                 *, emit: Callable[[str, str, str], None] | None = None,
                 secret: str = "", on_usage: Callable[[int, int, int], None] | None = None,
                 label: str = "", on_rate_limit: Callable[[], None] | None = None,
                 prior_calls: int = 0) -> None:
        self._factory = client_factory
        self._limits = limits
        self._cancel = cancel
        self._emit = emit or (lambda agent, kind, text: None)
        self._secret = secret
        self._on_usage = on_usage or (lambda calls, tin, tout: None)
        self._on_rate_limit = on_rate_limit or (lambda: None)
        self.label = label
        self._client: Any = None
        self._lock = threading.Lock()
        self._calls = 0
        self._prior = max(0, int(prior_calls))
        self._slots = threading.Semaphore(limits.max_concurrency)

    @property
    def calls_made(self) -> int:
        return self._calls

    def _client_or_raise(self) -> Any:
        with self._lock:
            if self._client is None:
                try:
                    self._client = self._factory()
                except TeamError:
                    raise
                except ClaudeError as exc:
                    raise TeamError(ErrorKind.NO_CREDENTIAL, redact(exc.message, self._secret)) from exc
            return self._client

    def _take_budget(self) -> None:
        with self._lock:
            if self._calls >= self._limits.max_model_calls:
                raise TeamError(
                    ErrorKind.BUDGET,
                    f"Stopped: this mission reached its limit of {self._limits.max_model_calls} "
                    "model calls. Raise it in Team settings if that is too low.")
            lifetime = self._limits.lifetime_calls
            if self._prior + self._calls >= lifetime:
                raise TeamError(
                    ErrorKind.BUDGET,
                    f"Stopped: this mission has used {self._prior + self._calls} model calls across its runs "
                    f"(the cap is {lifetime}: {self._limits.LIFETIME_RUNS} runs' worth). Start a new mission, or raise "
                    "the limit in Team settings.")
            self._calls += 1

    def complete(self, system: str, user: str, *, agent: str, purpose: str = "") -> Reply:
        """One model call (with bounded retries). Raises TeamError or Cancelled."""
        attempt = 0
        while True:
            self._cancel.raise_if_cancelled()
            client = self._client_or_raise()
            self._take_budget()
            acquired = False
            while not acquired:
                self._cancel.raise_if_cancelled()
                acquired = self._slots.acquire(timeout=0.1)
            try:
                response = client.send(
                    system=system, messages=[{"role": "user", "content": user}], tools=[])
            except ClaudeError as exc:
                kind = classify_error(exc)
                if kind == ErrorKind.RATE_LIMIT:
                    self._on_rate_limit()
                retryable = bool(exc.retryable) and kind in (
                    ErrorKind.RATE_LIMIT, ErrorKind.NETWORK, ErrorKind.PROVIDER)
                if retryable and attempt < self._limits.max_retries:
                    attempt += 1
                    delay = exc.retry_after if exc.retry_after else min(
                        2.0 ** attempt, self._limits.max_backoff_s)
                    delay = max(0.0, min(float(delay), self._limits.max_backoff_s))
                    why = "rate limited" if kind == ErrorKind.RATE_LIMIT else "had a temporary error"
                    self._emit(agent, "warning",
                               f"{self.label or 'The provider'} {why}; retrying in "
                               f"{delay:.0f}s (attempt {attempt}/{self._limits.max_retries}).")
                    if self._cancel.wait(delay):
                        raise Cancelled() from exc
                    continue
                message = redact(exc.message, self._secret)
                if kind == ErrorKind.RATE_LIMIT:
                    message = (f"{self.label or 'The provider'} kept rate-limiting requests; gave up "
                               f"after {self._limits.max_retries} retries. Wait a minute and Retry, "
                               "or lower concurrency in Team settings.")
                raise TeamError(kind, message, float(exc.retry_after or 0.0)) from exc
            finally:
                if acquired:
                    self._slots.release()
            text = (getattr(response, "text", "") or "").strip()
            self._on_usage(
                1, int(getattr(response, "input_tokens", 0) or 0),
                int(getattr(response, "output_tokens", 0) or 0))
            if not text:
                raise TeamError(ErrorKind.BAD_OUTPUT,
                                f"{agent.title()} got an empty reply from the model.")
            return Reply(redact(text, self._secret),
                         int(getattr(response, "input_tokens", 0) or 0),
                         int(getattr(response, "output_tokens", 0) or 0))


# ---------------------------------------------------------------------------
# Wiring to the browser's existing provider/credential layer
# ---------------------------------------------------------------------------

DEFAULT_TEAM_PROVIDER = "groq"
#: Used only when neither the Team nor the browser has a Groq model chosen.
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"


@dataclass
class ProviderStatus:
    """What the panel shows before a run: honest, secret-free."""

    provider: str
    label: str
    model: str
    available: bool
    detail: str
    #: Never part of repr(): a stray log/print of this object must not leak the key.
    secret: str = field(default="", repr=False)


#: Tried, in this order, when nothing was chosen explicitly: providers a person can use on a
#: free tier come first so a first run lands on the cheapest working option.
_FALLBACK_ORDER = ("groq", "gemini", "openrouter", "openai", "anthropic")


def resolve_provider(settings=None, env: dict | None = None) -> ProviderStatus:
    """Resolve provider, model and credential exactly as the rest of the
    browser does. Never raises; ``available`` False explains why.

    Which provider: ``PYBROWSER_TEAM_PROVIDER`` / the Team's own ``team_provider`` if set;
    otherwise the one chosen in Configure AI Agent (so a person sets up ONE provider and
    the assistant, AI Mode and the Team all use it); otherwise the first provider that has
    a key. Every provider the assistant supports works here, local models included.
    """
    import os

    from app.agent.config import (
        KEY_AGENT_PROVIDER, LOCAL_PROVIDER_IDS, PROVIDER_IDS, model_settings_key,
    )
    from app.agent.config import describe_provider
    from app.agent.credentials import PROVIDER_KEY_INFO, resolve_for

    source = env if env is not None else os.environ

    def stored(key: str, default: str = "") -> str:
        try:
            return (settings.get(key, default) if settings is not None else default) or default
        except Exception:  # noqa: BLE001
            return default

    def usable(candidate: str) -> bool:
        if candidate in LOCAL_PROVIDER_IDS:
            return True            # no key; whether it answers is what Test connection shows
        return bool(resolve_for(candidate).available)

    explicit = (source.get("PYBROWSER_TEAM_PROVIDER") or stored("team_provider", "")).strip().lower()
    provider = explicit if explicit in PROVIDER_IDS else ""
    if not provider:
        chosen = stored(KEY_AGENT_PROVIDER, "").strip().lower()
        if chosen in PROVIDER_IDS and usable(chosen):
            provider = chosen
    if not provider:
        provider = next((p for p in _FALLBACK_ORDER if usable(p)), DEFAULT_TEAM_PROVIDER)
    label = PROVIDER_KEY_INFO.get(provider, (None,))[0] or describe_provider(provider).label
    local = provider in LOCAL_PROVIDER_IDS
    model = (source.get("PYBROWSER_TEAM_MODEL") or stored("team_model", "")
             or stored(model_settings_key(provider), "")
             or (DEFAULT_GROQ_MODEL if provider == DEFAULT_TEAM_PROVIDER else "")).strip()
    if not model and provider == "anthropic":
        from app.agent.config import DEFAULT_MODEL
        model = DEFAULT_MODEL
    if not model:
        return ProviderStatus(provider, label, "", False,
                              f"No {label} model is chosen. Pick one in Tools → AI → Configure AI Agent.")
    if local:
        return ProviderStatus(provider, label, model, True,
                              "A model running on this computer; nothing is sent to a cloud service. "
                              "Quality varies a lot by model - team runs need one that follows "
                              "formatting instructions well.")
    credential = resolve_for(provider)
    if not credential.available:
        env_var = PROVIDER_KEY_INFO.get(provider, ("", "", ""))[1]
        return ProviderStatus(
            provider, label, model, False,
            f"No {label} API key is configured. Add one in Tools → AI → Configure AI Agent "
            f"(stored in your OS keyring), or set the {env_var} environment variable "
            "before starting the browser. Gemini, Groq and OpenRouter have free tiers; "
            "a local model (Ollama, LM Studio) needs no key.")
    return ProviderStatus(provider, label, model, True, credential.describe(),
                          secret=credential.secret or "")


def make_client_factory(status: ProviderStatus, settings=None, limits: TeamLimits | None = None):
    """A zero-argument factory building the provider client on first use, through the same
    dispatch the assistant uses (``build_transport``), so every supported provider works."""

    def factory():
        from types import SimpleNamespace

        from app.agent.config import LOCAL_PROVIDER_IDS, AgentConfig
        from app.agent.credentials import resolve_for
        from app.ui.agent_setup import build_transport

        if not status.available:
            raise TeamError(ErrorKind.NO_CREDENTIAL, status.detail)
        config = AgentConfig.from_environment(settings)
        config.provider = status.provider
        config.model = status.model
        config.max_tokens = (limits or TeamLimits()).max_output_tokens
        config.request_timeout_s = 90.0
        if status.provider in LOCAL_PROVIDER_IDS:
            from app.agent.config import default_local_endpoint, local_endpoint_settings_key
            try:
                config.local_endpoint = ((settings.get(local_endpoint_settings_key(status.provider), "")
                                          if settings is not None else "")
                                         or default_local_endpoint(status.provider)).strip()
            except Exception:  # noqa: BLE001
                config.local_endpoint = default_local_endpoint(status.provider)
            credential = SimpleNamespace(provider=status.provider, secret="")
        else:
            credential = resolve_for(status.provider)
        return build_transport(credential, config)

    return factory
