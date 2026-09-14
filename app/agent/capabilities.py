"""What a model can actually do - tri-state, never guessed from its name.

Phase 18's capability model (Parts 5-6). A local runtime's own metadata is
often thin or absent, so every capability question here has three possible
answers, not two:

* ``SUPPORTED`` - confirmed, either by the runtime's own metadata or by a
  real probe request that got a real structured result back.
* ``UNSUPPORTED`` - confirmed absent, the same way.
* ``UNKNOWN`` - genuinely not known. This is the *safe default* - Part 5 is
  explicit that uncertainty must never be resolved by guessing from a model
  name ("qwen-coder probably does tools"). Callers that gate a privileged
  capability (tool use - see Part 10) treat UNKNOWN as "do not assume yes."

Probing (Part 6) is a real request, not a heuristic: a tiny message plus one
harmless fake tool, and the only thing that counts as "supported" is the
runtime returning a genuine structured tool call naming that exact tool -
never text that merely mentions a tool by name. Results are cached by
(endpoint, model) so a probe never repeats on every ordinary chat request;
``CapabilityCache.invalidate`` is the explicit "Refresh" action.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class Capability:
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

    ALL = (SUPPORTED, UNSUPPORTED, UNKNOWN)


#: The one harmless, read-only, clearly-fake tool ever offered during a
#: probe. Named unmistakably so no real conversation would ever call it by
#: accident, and described as a no-op so a model has no reason not to call
#: it if it can call anything at all.
_PROBE_TOOL_NAME = "pybrowser_capability_probe"
_PROBE_TOOL = {
    "name": _PROBE_TOOL_NAME,
    "description": "Call this exact tool with no arguments. It does nothing "
                   "and is used only to check whether tool calling works.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

#: A 1x1 transparent PNG, the smallest possible real image - used only to
#: probe whether an endpoint accepts an image at all (Part 6: "using a tiny
#: generated image"). Never sent to a cloud provider; the vision probe is
#: only ever run against a local endpoint the user explicitly configured.
_TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class ModelCapabilities:
    """What is known about one model at one endpoint. Every field is
    conservative-by-default: a freshly-created instance with no other
    information is all UNKNOWN, never assumed SUPPORTED."""

    tools: str = Capability.UNKNOWN
    vision: str = Capability.UNSUPPORTED
    structured_output: str = Capability.UNKNOWN
    #: Streaming is a wire-format property this codebase implements for
    #: every OpenAI-compatible client uniformly (see OpenAICompatibleClient.
    #: send()'s on_text parameter) - not a per-model unknown the way tool
    #: calling and vision are, so this is a plain bool rather than a
    #: tri-state, defaulting to True (streaming is always attempted; a
    #: runtime that cannot stream simply returns its answer as one final
    #: chunk, which the existing streaming path already handles).
    streaming: bool = True
    #: None when not known - never guessed at a round number.
    context_length: int | None = None
    #: Where this record came from - "metadata" (the runtime told us),
    #: "probe" (a real request confirmed it), or "" (no information at
    #: all, the all-UNKNOWN default). Shown in the UI so a person can tell
    #: a confirmed answer from an assumed one.
    source: str = ""
    #: Unix time this record was produced - part of the cache key story:
    #: a manual refresh replaces this outright rather than merging.
    probed_at: float = 0.0
    #: The runtime/version string reported at probe time, when available -
    #: part of Part 6's cache key (endpoint, model, runtime/version), so a
    #: runtime upgrade that changes what a model can do does not keep
    #: serving a stale answer forever (still requires an explicit refresh;
    #: this field is diagnostic, not itself a trigger for one).
    runtime: str = ""

    def blocks_tools(self) -> bool:
        """True only when tool use is *confirmed* unsupported - Part 10's
        distinction between "cannot" (block) and "unknown" (let the real
        request be the test, same as every cloud provider today)."""
        return self.tools == Capability.UNSUPPORTED

    def blocks_vision(self) -> bool:
        return self.vision == Capability.UNSUPPORTED

    def blocks_structured_output(self) -> bool:
        """True only when structured output is *confirmed* unsupported -
        the same conservative rule as blocks_tools/blocks_vision. Since no
        provider in this codebase offers provider-side constrained
        decoding today (see Skill.output_schema's own docstring), this is
        never about "can it produce JSON at all" - every model gets the
        same prompt+validate fallback (Part 12). It is about whether a
        model has been confirmed unable to even follow a structured-
        response instruction reliably (the same signal a tool-call probe
        already produces - see probe_capabilities, which sets
        structured_output from the tool-call probe result)."""
        return self.structured_output == Capability.UNSUPPORTED


def default_capabilities() -> ModelCapabilities:
    return ModelCapabilities()


# ---------------------------------------------------------------------------
# Cache - keyed by (endpoint, model), optionally persisted via SettingsStore
# ---------------------------------------------------------------------------


#: A process-wide cache, shared by every caller that does not have (or need)
#: its own SettingsStore-backed instance - the UI call sites that only ask
#: "is vision confirmed unsupported for the currently selected model?"
#: (agent_panel.py) use this rather than threading a settings reference
#: through every constructor just for this one lookup. MainWindow's own
#: capability-probing flows (Tools -> Configure AI Agent) may still build a
#: settings-backed CapabilityCache directly when persistence across
#: restarts matters more than call-site simplicity.
_default_cache: "CapabilityCache | None" = None


def default_cache() -> "CapabilityCache":
    global _default_cache
    if _default_cache is None:
        _default_cache = CapabilityCache()
    return _default_cache


def _key(endpoint: str, model: str) -> str:
    return f"{(endpoint or '').rstrip('/')}|{(model or '').strip()}"


class CapabilityCache:
    """In-memory cache of capability probes, optionally backed by a
    SettingsStore so results survive a restart.

    Never probes anything itself - see probe_tool_support/probe_vision_
    support below, which this cache wraps in ``get_or_probe``. The whole
    point of this class is Part 6's "never repeatedly probe on every
    request": once a (endpoint, model) pair has an answer, every later
    caller gets the cached one until ``invalidate`` (the "Refresh" action)
    is called explicitly.
    """

    _SETTINGS_KEY = "local_model_capabilities"

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self._cache: dict[str, ModelCapabilities] = {}
        self._load()

    def _load(self) -> None:
        if self._settings is None:
            return
        try:
            import json

            raw = self._settings.get(self._SETTINGS_KEY, "") or ""
            if not raw:
                return
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            for key, record in data.items():
                if isinstance(record, dict):
                    self._cache[key] = ModelCapabilities(
                        tools=record.get("tools", Capability.UNKNOWN),
                        vision=record.get("vision", Capability.UNSUPPORTED),
                        structured_output=record.get("structured_output", Capability.UNKNOWN),
                        streaming=bool(record.get("streaming", True)),
                        context_length=record.get("context_length"),
                        source=record.get("source", ""),
                        probed_at=float(record.get("probed_at", 0.0)),
                        runtime=record.get("runtime", ""),
                    )
        except Exception:  # noqa: BLE001 - a corrupted cache is never fatal
            pass

    def _persist(self) -> None:
        if self._settings is None:
            return
        try:
            import json
            from dataclasses import asdict

            data = {key: asdict(value) for key, value in self._cache.items()}
            self._settings.set(self._SETTINGS_KEY, json.dumps(data))
        except Exception:  # noqa: BLE001 - persistence is never load-bearing
            pass

    def get(self, endpoint: str, model: str) -> ModelCapabilities | None:
        return self._cache.get(_key(endpoint, model))

    def set(self, endpoint: str, model: str, capabilities: ModelCapabilities) -> None:
        self._cache[_key(endpoint, model)] = capabilities
        self._persist()

    def invalidate(self, endpoint: str, model: str | None = None) -> None:
        """Clear one model's cached capabilities, or - with ``model`` None -
        every entry for that endpoint. The explicit "manual refresh" Part 6
        requires."""
        if model is not None:
            self._cache.pop(_key(endpoint, model), None)
        else:
            prefix = f"{(endpoint or '').rstrip('/')}|"
            for key in [k for k in self._cache if k.startswith(prefix)]:
                del self._cache[key]
        self._persist()


# ---------------------------------------------------------------------------
# Probing - real requests, real structured results only
# ---------------------------------------------------------------------------


def probe_tool_support(client, *, timeout_note: str = "") -> str:
    """Send one tiny request offering exactly one harmless, unmistakably
    fake tool, and check whether the runtime returns a genuine structured
    tool call naming it.

    Returns a Capability value. Never raises: a probe that fails to even
    connect answers UNKNOWN, not UNSUPPORTED - "could not tell" is not the
    same fact as "confirmed absent." The one thing this function is
    absolute about (Part 6's explicit warning) is that free-form text
    mentioning the tool's name is never accepted as evidence - only
    ``response.tool_calls`` containing a call whose name matches exactly.
    """
    try:
        response = client.send(
            system="You must call the tool named pybrowser_capability_probe with no "
                  "arguments. Do not answer in words.",
            messages=[{"role": "user", "content": "Call the tool now."}],
            tools=[_PROBE_TOOL],
        )
    except Exception:  # noqa: BLE001 - connectivity/API failure, not a capability fact
        return Capability.UNKNOWN
    for call in response.tool_calls:
        if call.name == _PROBE_TOOL_NAME:
            return Capability.SUPPORTED
    # A real response came back with no structured call for the tool we
    # offered - that is a genuine "no", not an unknown.
    return Capability.UNSUPPORTED


def probe_vision_support(client) -> str:
    """Send one tiny image and see whether the runtime accepts it at all.

    Deliberately narrow: this can only ever confirm "the endpoint did not
    reject an image outright" - it cannot verify the model actually *saw*
    anything meaningful in a 1x1 pixel, so a SUPPORTED result here means
    "the wire format works," which is exactly the question a local vision
    model integration needs answered before ever sending a real
    screenshot. Never raises - see probe_tool_support's docstring for the
    same UNKNOWN-on-failure reasoning.
    """
    try:
        response = client.send(
            system="",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "What color is this image? Answer in one word."},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": _TINY_PNG_BASE64}},
            ]}],
            tools=[],
        )
    except Exception as exc:  # noqa: BLE001
        # ClaudeError's own str() is the friendly, generic sentence shown
        # to a person ("... rejected the request (400)") - the API's own
        # explanation of *why* lives in api_message (see ClaudeError's own
        # docstring on that split), which is where a real "does not
        # support vision" rejection actually says so.
        message = (str(exc) + " " + str(getattr(exc, "api_message", "") or "")).lower()
        if any(word in message for word in ("image", "vision", "multimodal", "modality")):
            return Capability.UNSUPPORTED
        return Capability.UNKNOWN
    return Capability.SUPPORTED if (response.text or response.tool_calls) else Capability.UNKNOWN


def probe_capabilities(client, *, endpoint: str = "", model: str = "",
                       include_vision: bool = False) -> ModelCapabilities:
    """Run the probes and package the result. ``include_vision`` defaults
    False - Part 6: "Vision probe: only if needed", since it is a second
    real request and most callers only care about tool support."""
    tools = probe_tool_support(client)
    vision = Capability.UNKNOWN if include_vision else Capability.UNSUPPORTED
    if include_vision:
        vision = probe_vision_support(client)
    return ModelCapabilities(
        tools=tools, vision=vision, structured_output=tools,
        source="probe", probed_at=time.time())


def get_or_probe(cache: CapabilityCache, client, *, endpoint: str, model: str,
                 include_vision: bool = False, force: bool = False) -> ModelCapabilities:
    """The one entry point most callers use: cached if present, else a real
    probe, cached for next time. ``force=True`` is the manual-refresh path."""
    if not force:
        cached = cache.get(endpoint, model)
        if cached is not None:
            return cached
    result = probe_capabilities(client, endpoint=endpoint, model=model,
                                include_vision=include_vision)
    cache.set(endpoint, model, result)
    return result


# ---------------------------------------------------------------------------
# Metadata-based capabilities - no probe, from what the runtime already told us
# ---------------------------------------------------------------------------


def capabilities_from_ollama_entry(entry: dict[str, Any]) -> ModelCapabilities:
    """Ollama's ``/api/show`` response for a model can report a
    ``capabilities`` array (e.g. ``["completion", "tools", "vision"]``) on
    builds that support it. Used only when present - never inferred from
    the model's name (Part 5's explicit warning)."""
    reported = entry.get("capabilities")
    if not isinstance(reported, list):
        return ModelCapabilities(source="")
    tools = Capability.SUPPORTED if "tools" in reported else Capability.UNSUPPORTED
    vision = Capability.SUPPORTED if "vision" in reported else Capability.UNSUPPORTED
    return ModelCapabilities(tools=tools, vision=vision, structured_output=tools,
                             source="metadata", probed_at=time.time())


# ---------------------------------------------------------------------------
# Gating - Part 10: never let free-form text stand in for a tool call
# ---------------------------------------------------------------------------

TOOL_UNSUPPORTED_USER_MESSAGE = (
    "This model cannot use browser tools. Choose a tool-capable model to run this task.")


def effective_vision_support(config, cache: "CapabilityCache | None" = None) -> bool:
    """Whether an image should actually be sent for this config's provider
    and model right now - Part 11: preserve the existing explicit warning
    for a non-vision model, never silently strip the image instead.

    For a cloud provider this is unchanged - the existing provider-level
    ``provider_supports_images`` flag. For a local provider it additionally
    checks the capability cache: a *confirmed* UNSUPPORTED blocks (the
    warning banner then fires exactly as it always has), but UNKNOWN - the
    common case before anything has been probed - still passes through,
    the same "let the real request be the proof" default every cloud
    provider's own unconfirmed-per-model vision support already gets.
    """
    from app.agent.config import is_local_provider, provider_supports_images

    provider_level = provider_supports_images(config.provider)
    if not provider_level or not is_local_provider(config.provider) or cache is None:
        return provider_level
    caps = cache.get(config.local_endpoint, config.model)
    if caps is not None and caps.blocks_vision():
        return False
    return True


def require_tool_capability(capabilities: ModelCapabilities | None) -> str | None:
    """None if a tool-requiring task may proceed; otherwise the exact
    message to show and refuse with. Only a *confirmed* UNSUPPORTED blocks -
    UNKNOWN is treated the same as every cloud provider always has been:
    let the real request be the proof, and lean on the existing 400-based
    TOOL_UNSUPPORTED_MESSAGE handling in openai_compatible.py if it turns
    out not to work."""
    if capabilities is not None and capabilities.blocks_tools():
        return TOOL_UNSUPPORTED_USER_MESSAGE
    return None


# ---------------------------------------------------------------------------
# Part 13/14/23: local-privacy status - visible, honest, and narrow
# ---------------------------------------------------------------------------

#: Part 13's exact required wording, plus the caveat the phase brief is
#: explicit must NOT be dropped: local inference says nothing about a
#: website's own network access, an MCP server's own backend, or a
#: connected app's own behaviour. Never "PyBrowser is completely offline" -
#: that claim is not true and this module must never make it.
LOCAL_PRIVACY_MESSAGE = (
    "Model runs locally on this computer. Prompts and context sent to it "
    "stay on this machine. This does not mean PyBrowser itself is "
    "offline - websites you visit, MCP servers, and connected apps may "
    "still use the network.")


def is_inference_local(config) -> bool:
    """True only when the configured provider is a local one *and* its
    actual configured endpoint is genuinely local (Part 23: a "local"
    provider pointed at a remote URL is still remote - this checks the
    real host, never the provider flag alone)."""
    from app.agent.config import is_local_provider
    from app.agent.local_providers import is_local_endpoint

    return is_local_provider(config.provider) and is_local_endpoint(config.local_endpoint or "")


def local_privacy_status(config) -> str | None:
    """The Part 13 status line, or None when inference is not genuinely
    local - never shown for a cloud provider or a local-provider type
    pointed at a remote endpoint."""
    return LOCAL_PRIVACY_MESSAGE if is_inference_local(config) else None


def should_show_cloud_egress_warning(config) -> bool:
    """Part 14: a cloud-egress warning is about data leaving this machine
    for a third party - genuinely meaningless when the destination is a
    localhost model endpoint. Every other Phase 15 protection (secret
    redaction, provenance, prompt-injection defenses, action approvals)
    stays on regardless - this function gates only the one warning that
    is specifically about *the model itself* being a cloud destination,
    never the redaction/approval pipeline, which does not call this at
    all (see app/security/firewall.py - it redacts unconditionally)."""
    return not is_inference_local(config)


# ---------------------------------------------------------------------------
# Part 16: Skills may prefer a local model - validate before running
# ---------------------------------------------------------------------------


def skill_requires_tools(skill) -> bool:
    """See Skill.allowed_tools's own docstring: None or a non-empty tuple
    both mean "this Skill can call at least one tool"; an explicit empty
    tuple means it was scoped to none on purpose."""
    return skill.allowed_tools is None or len(skill.allowed_tools) > 0


def skill_requires_vision(skill) -> bool:
    return "image" in (skill.default_context_kinds or ())


def skill_requires_structured_output(skill) -> bool:
    return skill.output_schema is not None


def validate_skill_capability(skill, capabilities: ModelCapabilities | None) -> str | None:
    """None if this Skill may run against a model with these capabilities;
    otherwise the message to show and refuse with - Part 16: "If
    incompatible: ask user to choose another model." Only a *confirmed*
    UNSUPPORTED blocks, the same conservative rule require_tool_capability
    uses - UNKNOWN is never treated as a refusal.
    """
    if capabilities is None:
        return None
    if skill_requires_tools(skill) and capabilities.blocks_tools():
        return (f'"{skill.name}" needs tool calling, which the selected model does not '
               "support. Choose a different model to run this Skill.")
    if skill_requires_vision(skill) and capabilities.blocks_vision():
        return (f'"{skill.name}" needs a vision-capable model, and the selected one is not. '
               "Choose a different model to run this Skill.")
    if skill_requires_structured_output(skill) and capabilities.blocks_structured_output():
        return (f'"{skill.name}" needs a structured JSON response, and the selected model is '
               "confirmed unable to follow that reliably. Choose a different model to run "
               "this Skill.")
    return None


# ---------------------------------------------------------------------------
# Part 20: grouping discovered models by real, known capability
# ---------------------------------------------------------------------------


def group_models_by_capability(
    entries: list[tuple[str, ModelCapabilities]],
) -> dict[str, list[str]]:
    """Bucket model ids into "Tool-capable", "Vision", and "Chat" groups,
    from real metadata/probed capability only - never a marketing guess
    from the name. A model can appear in more than one bucket; every model
    appears in "Chat" at minimum, since text completion is the one thing
    every chat model in this list can do."""
    groups: dict[str, list[str]] = {"Tool-capable": [], "Vision": [], "Chat": []}
    for model_id, caps in entries:
        groups["Chat"].append(model_id)
        if caps.tools == Capability.SUPPORTED:
            groups["Tool-capable"].append(model_id)
        if caps.vision == Capability.SUPPORTED:
            groups["Vision"].append(model_id)
    return {name: models for name, models in groups.items() if models}
