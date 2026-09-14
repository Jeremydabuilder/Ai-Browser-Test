"""Local AI models: Ollama, LM Studio, and generic OpenAI-compatible runtimes.

Phase 18's core architecture rule: reuse the existing provider abstraction.
There is no separate "local agent" engine here - every client in this module
implements the exact same ``send()`` contract as ``ClaudeClient`` and the
cloud ``OpenAICompatibleClient`` subclasses (``ClaudeTransport`` in
claude_client.py), so ``AgentSession``, ``ToolRegistry``, Missions, Skills,
MCP and the privacy firewall never know or care that a request is going to
a process running on the same machine instead of a cloud API.

What is genuinely different about a local runtime, and why each gets its
own class rather than one "local" class:

* **Ollama** exposes both its own native API (``/api/tags``, ``/api/chat``)
  and, since 0.1.24, an OpenAI-compatible ``/v1/chat/completions`` /
  ``/v1/models`` surface. Chat reuses the OpenAI-compatible wire format
  (inherited from ``OpenAICompatibleClient`` unchanged); model discovery
  uses the native ``/api/tags`` endpoint instead, because it is the one
  that actually lists what is installed on disk - ``/v1/models`` is a
  thinner mirror of the same data on older Ollama builds and is not always
  present.
* **LM Studio**'s local server is OpenAI-compatible end to end - chat and
  model discovery both reuse the base class unchanged. The only thing that
  differs from a cloud provider is that the base URL is user-configured
  (LM Studio's own default port, 1234, is not a cloud constant this
  codebase can hardcode a route for) and no API key is required.
* **Generic Local OpenAI-compatible** is exactly what its name says: a
  fully user-configured base URL and model id, with an *optional* API key
  for a runtime that happens to require one (some vLLM/LocalAI deployments
  do). No project-specific quirks are assumed - see the module docstring
  in openai_compatible.py's OpenAICompatibleClient for why that adapter
  already carries every piece of translation logic these two need.

Every class here is a thin ``OpenAICompatibleClient`` subclass. The base
class assumes a fixed, class-level ``base_url`` and always requires an API
key - both true for every cloud provider, neither true for a local one -
so ``LocalOpenAICompatibleClient`` overrides construction to accept an
instance-level base URL and to treat a missing key as normal rather than
an error, without touching the cloud-provider base class at all.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx

from app.agent.claude_client import ClaudeError
from app.agent.config import (
    LOCAL_PROVIDER_IDS,
    PROVIDER_LM_STUDIO,
    PROVIDER_LOCAL_OPENAI,
    PROVIDER_OLLAMA,
    default_local_endpoint,
)
from app.agent.openai_compatible import OpenAICompatibleClient, pretty_label

# ---------------------------------------------------------------------------
# Provider identity - re-exported from config.py, the single source of
# truth, so existing callers of this module need not import config too.
# ---------------------------------------------------------------------------

__all__ = [
    "PROVIDER_OLLAMA", "PROVIDER_LM_STUDIO", "PROVIDER_LOCAL_OPENAI", "LOCAL_PROVIDER_IDS",
    "DEFAULT_ENDPOINTS", "LocalEndpointError", "is_local_endpoint", "validate_endpoint",
    "LocalOpenAICompatibleClient", "LMStudioClient", "OllamaClient", "OllamaModel",
]

#: What "not configured yet" looks like for each local provider's endpoint
#: field - see app.agent.config.default_local_endpoint, the single source
#: of truth this is built from.
DEFAULT_ENDPOINTS: dict[str, str] = {
    provider_id: default_local_endpoint(provider_id) for provider_id in LOCAL_PROVIDER_IDS
}


# ---------------------------------------------------------------------------
# Endpoint validation - Part 23: treat a user-entered endpoint as potentially
# untrusted, and classify "local" narrowly rather than by guesswork.
# ---------------------------------------------------------------------------


class LocalEndpointError(ValueError):
    """A user-entered endpoint is not usable, with a reason a person can read."""


#: The only hostnames ever considered "local" for the purposes of privacy
#: classification (Part 23: "Do NOT automatically classify arbitrary
#: LAN/public URLs as private/local"). A LAN address (192.168.x.x, a
#: hostname on the local network) is still treated as remote: it is
#: reachable off this machine, which is exactly the property "local" is
#: meant to rule out.
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _hostname_of(url: str) -> str:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


def is_local_endpoint(url: str) -> bool:
    """True only for localhost / 127.0.0.1 / ::1 - see the module docstring
    and Part 23. A bare IP is also checked numerically (covers 127.0.0.2
    etc., the whole IPv4 loopback block, and any IPv6 loopback spelling),
    since "127.x.x.x is loopback" is a real fact about the address, not a
    guess - everything else (a LAN IP, a hostname on the local network, a
    public domain) is remote.
    """
    host = _hostname_of(url)
    if not host:
        return False
    if host.strip("[]") in _LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_endpoint(url: str) -> str:
    """Reject anything that is not a plain http(s) URL naming a host.

    Part 23's SSRF-hardening ask: only http/https schemes, a real host, no
    ``file://``, no way to smuggle a command or a local path through what
    is supposed to be "an endpoint to send chat requests to." Returns the
    normalised (stripped, trailing-slash-free) URL on success; raises
    ``LocalEndpointError`` with a message safe to show the user otherwise.
    """
    raw = (url or "").strip()
    if not raw:
        raise LocalEndpointError("Enter an endpoint URL first.")
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise LocalEndpointError(f"'{raw}' is not a valid URL.") from exc
    if parsed.scheme not in ("http", "https"):
        raise LocalEndpointError(
            f"Only http:// and https:// endpoints are supported (got '{parsed.scheme or raw}').")
    if not parsed.hostname:
        raise LocalEndpointError(f"'{raw}' does not name a host.")
    return raw.rstrip("/")


# ---------------------------------------------------------------------------
# Base class shared by all three local clients
# ---------------------------------------------------------------------------


class LocalOpenAICompatibleClient(OpenAICompatibleClient):
    """A local runtime's OpenAI-compatible endpoint - configurable base URL,
    no API key required by default.

    This IS "Local OpenAI-compatible" (Part 3) used directly, and also the
    base every other local client in this module subclasses, since Ollama
    and LM Studio are both, at the wire level, exactly this with a
    different default endpoint and (for Ollama) a different model-discovery
    route.
    """

    label = "Local OpenAI-compatible"
    #: A locally-hosted vision model is exactly as real as a cloud one, and
    #: this adapter already knows how to build the image_url data-URI shape
    #: (see openai_compatible._user_content_turn) - whether a *specific*
    #: model can actually see the image is a per-model capability question
    #: answered by app.agent.capabilities, never something this class
    #: claims across the board.
    SUPPORTS_IMAGES = True

    def __init__(self, api_key: str, config, *, base_url: str | None = None,
                 transport: "httpx.BaseTransport | None" = None) -> None:
        #: Resolved before super().__init__ touches self.base_url, so the
        #: parent's httpx.Client is built against the right host from the
        #: very first line - a class-level base_url would otherwise win.
        endpoint = base_url or getattr(config, "local_endpoint", "") or self.base_url
        self.base_url = validate_endpoint(endpoint) if endpoint else ""
        if not self.base_url:
            raise ClaudeError(f"No endpoint is configured for {self.label}. "
                              "Set one in Tools → Configure AI Agent.")
        super().__init__(api_key or "placeholder-not-required", config, transport=transport)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = (self._api_key or "").strip()
        if key and key != "placeholder-not-required":
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # -- connection test: a real round trip, not just "the socket opened" --
    @classmethod
    def test_connection(cls, api_key: str, model: str, *, base_url: str | None = None,
                        timeout: float = 20.0,
                        transport: "httpx.BaseTransport | None" = None) -> tuple[bool, str]:
        endpoint = base_url or ""
        try:
            endpoint = validate_endpoint(endpoint)
        except LocalEndpointError as exc:
            return False, str(exc)
        if not (model or "").strip():
            return False, "Choose or enter a model first."
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ready."}],
            "max_tokens": 16,
        }
        headers = {"Content-Type": "application/json"}
        key = (api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            with httpx.Client(base_url=endpoint, timeout=timeout, transport=transport,
                              headers=headers) as client:
                response = client.post("/chat/completions", json=body)
        except httpx.TimeoutException:
            return False, (f"{cls.label} did not respond in time. It may still be "
                           "loading the model - try again in a moment.")
        except httpx.ConnectError:
            return False, cls._connect_error_message()
        except Exception as exc:  # noqa: BLE001 - reported to the user, not raised
            return False, f"Could not reach {cls.label}: {exc}"
        if response.status_code == 200:
            return True, "Connected. The endpoint accepted a chat request."
        from app.agent.openai_compatible import error_message

        message = error_message(response) or response.text[:300]
        if response.status_code == 404:
            return False, (f"The model '{model}' is not available at this endpoint. "
                           f"{message}".strip())
        return False, f"{cls.label} rejected the request ({response.status_code}): {message}"

    @classmethod
    def _connect_error_message(cls) -> str:
        """Part 21's example message, generalised per subclass."""
        return f"Could not reach {cls.label}. Check that it is running and the endpoint is correct."

    # -- model discovery: /v1/models, tolerant of it not existing ----------
    @classmethod
    def list_models(cls, api_key: str, *, base_url: str | None = None, timeout: float = 10.0,
                    transport: "httpx.BaseTransport | None" = None) -> list[dict[str, Any]]:
        """Never raises, and never fails the whole provider setup just
        because listing models is not supported - see Part 4. An empty
        list here always means "fall back to manual entry", never "this
        endpoint is broken" (that distinction is what test_connection is
        for)."""
        endpoint = base_url or ""
        try:
            endpoint = validate_endpoint(endpoint)
        except LocalEndpointError:
            return []
        try:
            with httpx.Client(base_url=endpoint, timeout=timeout, transport=transport) as client:
                response = client.get("/models")
                if response.status_code != 200:
                    return []
                data = response.json().get("data", [])
                return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
        except Exception:  # noqa: BLE001 - a listing failure is not fatal
            return []

    @classmethod
    def capability_of(cls, entry: dict[str, Any]) -> tuple[bool, str]:
        return True, "tool support unconfirmed - use Test Connection or capability probing"

    @classmethod
    def seed_models(cls) -> list[dict[str, str]]:
        return []


class LMStudioClient(LocalOpenAICompatibleClient):
    """https://lmstudio.ai - a local OpenAI-compatible server. Model
    discovery and chat both reuse the base class exactly: LM Studio's
    server implements ``/v1/models`` and ``/v1/chat/completions`` the same
    way any other OpenAI-compatible host does; the only local-specific
    things are the default port and "no API key needed"."""

    label = "LM Studio"

    @classmethod
    def _connect_error_message(cls) -> str:
        return "LM Studio doesn't appear to be running, or its local server isn't started. " \
               "Open LM Studio, start the local server, and try again."


@dataclass(frozen=True)
class OllamaModel:
    """One entry from Ollama's own ``/api/tags`` listing - kept separate
    from the plain ``{"id": ...}`` shape ``list_models()`` returns because
    Ollama's tags carry real, useful metadata (size, family, quantization)
    that a bare model id does not."""

    name: str
    size_bytes: int = 0
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""


class OllamaClient(LocalOpenAICompatibleClient):
    """https://ollama.com - chat over Ollama's OpenAI-compatible
    ``/v1/chat/completions`` (available since Ollama 0.1.24), model
    discovery over its native ``/api/tags`` (the one that reliably reflects
    what is actually installed on disk - see the module docstring).
    """

    label = "Ollama"

    def __init__(self, api_key: str, config, *, base_url: str | None = None,
                 transport: "httpx.BaseTransport | None" = None) -> None:
        # Ollama's native endpoints (api/tags, api/show) live at the bare
        # host; its OpenAI-compatible chat endpoint lives under /v1. The
        # *configured* endpoint is the bare host (what the user actually
        # types, and what DEFAULT_ENDPOINTS documents) - this class adds
        # /v1 only for the inherited OpenAI-compatible send() path.
        raw = base_url or getattr(config, "local_endpoint", "") or self.base_url
        self._native_base = validate_endpoint(raw) if raw else ""
        chat_base = f"{self._native_base}/v1" if self._native_base else None
        super().__init__(api_key, config, base_url=chat_base, transport=transport)

    @classmethod
    def _connect_error_message(cls) -> str:
        return ("Ollama is installed but doesn't appear to be running. "
                "Start Ollama and try again.")

    # -- model discovery: the native, disk-accurate listing -----------------
    @classmethod
    def list_installed(cls, base_url: str, *, timeout: float = 10.0,
                       transport: "httpx.BaseTransport | None" = None) -> list[OllamaModel]:
        """Every model actually installed on disk, per ``/api/tags``.
        Never raises - see Part 4."""
        try:
            endpoint = validate_endpoint(base_url)
        except LocalEndpointError:
            return []
        try:
            with httpx.Client(base_url=endpoint, timeout=timeout, transport=transport) as client:
                response = client.get("/api/tags")
                if response.status_code != 200:
                    return []
                payload = response.json()
        except Exception:  # noqa: BLE001
            return []
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return []
        out = []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or entry.get("model") or "").strip()
            if not name:
                continue
            details = entry.get("details") or {}
            out.append(OllamaModel(
                name=name,
                size_bytes=int(entry.get("size") or 0),
                family=str(details.get("family") or ""),
                parameter_size=str(details.get("parameter_size") or ""),
                quantization=str(details.get("quantization_level") or ""),
            ))
        return out

    @classmethod
    def list_models(cls, api_key: str, *, base_url: str | None = None, timeout: float = 10.0,
                    transport: "httpx.BaseTransport | None" = None,
                    include_capabilities: bool = False) -> list[dict[str, Any]]:
        """The shared ``{"id": ...}`` shape the settings dialog's model
        dropdown expects, built from the native listing above.

        ``include_capabilities`` (Phase 18 hardening) additionally calls
        ``/api/show`` per installed model and attaches its real, self-
        reported capability metadata (never guessed from the name) as
        each entry's ``"capabilities"`` key - see
        ``app.agent.capabilities.capabilities_from_ollama_entry``, which
        reads exactly that key. Off by default: this is one extra request
        per installed model, and Part 22 is explicit that nothing should
        eagerly probe every provider - callers that want live capability
        data ask for it explicitly (e.g. a model-list refresh in Settings),
        never on every ordinary discovery call.
        """
        if not base_url:
            return []
        entries = []
        for model in cls.list_installed(base_url, timeout=timeout, transport=transport):
            entry: dict[str, Any] = {"id": model.name, "ollama": model}
            if include_capabilities:
                show = cls.show_capabilities(base_url, model.name, timeout=timeout,
                                             transport=transport)
                if show is not None:
                    entry["capabilities"] = show.get("capabilities")
            entries.append(entry)
        return entries

    @classmethod
    def show_capabilities(cls, base_url: str, model: str, *, timeout: float = 10.0,
                          transport: "httpx.BaseTransport | None" = None
                          ) -> dict[str, Any] | None:
        """Ollama's ``/api/show`` for one installed model - its
        ``capabilities`` array (e.g. ``["completion", "tools", "vision"]``)
        is the live metadata ``app.agent.capabilities.
        capabilities_from_ollama_entry`` consumes. Returns the raw JSON
        payload, or None if the endpoint is unreachable or does not
        support this call on this Ollama build - never raises, matching
        every other discovery method in this module (Part 4: a listing
        failure must never fail provider setup)."""
        try:
            endpoint = validate_endpoint(base_url)
        except LocalEndpointError:
            return None
        try:
            with httpx.Client(base_url=endpoint, timeout=timeout, transport=transport) as client:
                response = client.post("/api/show", json={"model": model})
                if response.status_code != 200:
                    return None
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception:  # noqa: BLE001 - a capability-metadata miss is never fatal
            return None

    @staticmethod
    def _normalise_name(name: str) -> str:
        """'llama3' and 'llama3:latest' name the same installed model."""
        name = (name or "").strip().lower()
        return name if ":" in name else f"{name}:latest"

    @classmethod
    def is_installed(cls, base_url: str, model: str, *, timeout: float = 10.0,
                     transport: "httpx.BaseTransport | None" = None) -> bool:
        """Part 9: whether a chosen model is actually pulled - checked
        before running anything with it, never assumed from the name."""
        wanted = cls._normalise_name(model)
        installed = {cls._normalise_name(m.name) for m in
                    cls.list_installed(base_url, timeout=timeout, transport=transport)}
        return wanted in installed

    @staticmethod
    def pull_command(model: str) -> str:
        """Part 9: the correct guidance to show, never a command PyBrowser
        runs itself - installing a model is an explicit, user-initiated
        action taken in a terminal, not something a browser silently does
        on someone's disk."""
        return f"ollama pull {(model or '').strip()}"

    @classmethod
    def _connect_and_list(cls, base_url: str, *,
                          transport: "httpx.BaseTransport | None" = None) -> tuple[bool, str]:
        try:
            endpoint = validate_endpoint(base_url)
        except LocalEndpointError as exc:
            return False, str(exc)
        try:
            with httpx.Client(base_url=endpoint, timeout=10.0, transport=transport) as client:
                response = client.get("/api/tags")
        except httpx.ConnectError:
            return False, cls._connect_error_message()
        except httpx.TimeoutException:
            return False, "Ollama did not respond in time."
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not reach Ollama: {exc}"
        if response.status_code != 200:
            return False, f"Ollama returned an unexpected response ({response.status_code})."
        return True, "Ollama is running."

    @classmethod
    def test_connection(cls, api_key: str, model: str, *, base_url: str | None = None,
                        timeout: float = 20.0,
                        transport: "httpx.BaseTransport | None" = None) -> tuple[bool, str]:
        """Connection health first (Ollama running at all), then - only if
        a model was actually chosen - whether it can run a real chat
        request. A model missing at this endpoint gets Part 9's message
        rather than a generic 404, since "not installed" is the far more
        common and more actionable cause on Ollama specifically.
        """
        endpoint = base_url or ""
        ok, message = cls._connect_and_list(endpoint, transport=transport)
        if not ok:
            return False, message
        if not (model or "").strip():
            return True, "Ollama is running. Choose a model to test it."
        try:
            valid_endpoint = validate_endpoint(endpoint)
        except LocalEndpointError as exc:
            return False, str(exc)
        if not cls.is_installed(valid_endpoint, model, transport=transport):
            return False, (f"Model '{model}' is not installed. Run: {cls.pull_command(model)}")
        return super().test_connection(api_key, model, base_url=f"{valid_endpoint}/v1",
                                       timeout=timeout, transport=transport)
