"""Working out how to authenticate to Claude, without insisting on an API key.

An API key is the most familiar option and the worst one: it is a long-lived
secret the user has to paste somewhere and we then have to store. The Anthropic
SDK supports several alternatives, and this module finds whichever the user
already has.

Preference order, and why:

1. **A cloud backend** (Amazon Bedrock, Google Vertex AI), when the user asks
   for one with ``PYBROWSER_AGENT_BACKEND``. Authentication is the cloud
   provider's existing credentials - an IAM role, ``gcloud`` application
   default credentials - so there is no Anthropic secret at all.
2. **An API key stored in the OS keyring**, because the user explicitly put it
   there through this application's own dialog. An explicit choice wins over a
   discovered one.
3. **``ANTHROPIC_API_KEY``** from the environment, for headless and CI use.
4. **``ANTHROPIC_AUTH_TOKEN``** - a bearer token rather than a key.
5. **An OAuth profile on disk**, created by ``ant auth login``. Nothing is
   stored by us, the token refreshes itself, and it can be revoked centrally.
   This is the best option for a desktop browser and needs no key at all.

If none is present the agent stays unconfigured and the browser works normally.

Nothing here ever logs, displays, or returns a fragment of a secret. The
descriptions are about *where* a credential came from, never what it is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.agent.keys import ApiKeyStore

ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_BACKEND = "PYBROWSER_AGENT_BACKEND"


class Mode:
    KEYRING = "keyring"
    ENV_KEY = "env_key"
    AUTH_TOKEN = "auth_token"
    OAUTH_PROFILE = "oauth_profile"
    BEDROCK = "bedrock"
    VERTEX = "vertex"
    NONE = "none"


#: What to tell the user about each way in, and how to set it up.
SETUP_HELP: dict[str, str] = {
    Mode.OAUTH_PROFILE: "Sign in with the Anthropic CLI: `ant auth login`. "
                        "No key is stored by this browser.",
    Mode.KEYRING: "Stored in your operating system's keyring by this browser.",
    Mode.ENV_KEY: f"Set the {ENV_API_KEY} environment variable before launching.",
    Mode.AUTH_TOKEN: f"Set the {ENV_AUTH_TOKEN} environment variable before launching.",
    Mode.BEDROCK: f"Set {ENV_BACKEND}=bedrock and AWS_REGION; "
                  "authentication uses your existing AWS credentials.",
    Mode.VERTEX: f"Set {ENV_BACKEND}=vertex, GOOGLE_CLOUD_PROJECT and "
                 "GOOGLE_CLOUD_REGION; authentication uses gcloud "
                 "application default credentials.",
}


@dataclass(frozen=True)
class Credential:
    """How to authenticate. Carries the secret only when there is one to carry."""

    mode: str
    label: str                       # shown in the UI; never contains a secret
    secret: str | None = None        # api key or bearer token, if applicable
    region: str = ""
    project: str = ""

    @property
    def available(self) -> bool:
        return self.mode != Mode.NONE

    @property
    def needs_no_secret(self) -> bool:
        """True when nothing secret is held by this application."""
        return self.mode in (Mode.OAUTH_PROFILE, Mode.BEDROCK, Mode.VERTEX)

    def describe(self) -> str:
        return self.label


def _has_oauth_profile() -> bool:
    """Has the user signed in with `ant auth login`?

    The SDK owns the config-directory layout, so ask it rather than guessing
    at paths that could change underneath us.
    """
    try:
        from anthropic.lib.credentials._constants import (
            _has_active_profile_config,
            _has_explicit_active_config,
        )
    except Exception:  # noqa: BLE001 - older or restructured SDK
        return False
    try:
        if os.environ.get("ANTHROPIC_PROFILE") or os.environ.get("ANTHROPIC_CONFIG_DIR"):
            return True
        return bool(_has_explicit_active_config() or _has_active_profile_config())
    except Exception:  # noqa: BLE001
        return False


def _cloud_backend() -> Credential | None:
    """An explicitly requested cloud backend, if it is properly configured."""
    backend = (os.environ.get(ENV_BACKEND) or "").strip().lower()
    if backend == "bedrock":
        region = (os.environ.get("PYBROWSER_AWS_REGION")
                  or os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
        if not region:
            return None
        return Credential(Mode.BEDROCK, f"Amazon Bedrock ({region}), using your AWS credentials",
                          region=region)
    if backend == "vertex":
        project = (os.environ.get("PYBROWSER_GCP_PROJECT")
                   or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        region = (os.environ.get("PYBROWSER_GCP_REGION")
                  or os.environ.get("GOOGLE_CLOUD_REGION") or "global").strip()
        if not project:
            return None
        return Credential(Mode.VERTEX,
                          f"Google Vertex AI ({project}/{region}), using gcloud credentials",
                          region=region, project=project)
    return None


def resolve(store: ApiKeyStore | None = None) -> Credential:
    """Find the best credential this machine has.

    Never raises, for any reason. The window calls this while building the
    agent panel, and a credential lookup that throws would take the browser
    down with it - which has already happened once here, when a broken keyring
    backend raised a Rust panic rather than an Exception.
    """
    try:
        store = store or ApiKeyStore()
    except BaseException:  # noqa: BLE001
        store = None

    try:
        backend = _cloud_backend()
    except BaseException:  # noqa: BLE001
        backend = None
    if backend is not None:
        return backend

    try:
        keyring_key = store.get_keyring_key() if store is not None else None
    except BaseException:  # noqa: BLE001 - a broken keyring is not fatal
        keyring_key = None
    if keyring_key:
        return Credential(Mode.KEYRING, "API key from the OS keyring", secret=keyring_key)

    env_key = (os.environ.get(ENV_API_KEY) or "").strip()
    if env_key:
        return Credential(Mode.ENV_KEY, f"API key from {ENV_API_KEY}", secret=env_key)

    token = (os.environ.get(ENV_AUTH_TOKEN) or "").strip()
    if token:
        return Credential(Mode.AUTH_TOKEN, f"Bearer token from {ENV_AUTH_TOKEN}", secret=token)

    try:
        signed_in = _has_oauth_profile()
    except BaseException:  # noqa: BLE001 - see the guarantee in the docstring
        signed_in = False
    if signed_in:
        # The SDK reads and refreshes this itself; we never see the token.
        return Credential(Mode.OAUTH_PROFILE, "Signed in with the Anthropic CLI (no key stored)")

    return Credential(Mode.NONE, "no credential configured")


def options_summary() -> list[tuple[str, bool, str]]:
    """Every way in, whether it is currently available, and how to set it up.

    Used by the setup dialog so a user can see the alternatives to pasting a
    key rather than assuming a key is the only way.
    """
    store = ApiKeyStore()
    try:
        has_keyring_key = bool(store.get_keyring_key())
    except Exception:  # noqa: BLE001
        has_keyring_key = False
    return [
        (Mode.OAUTH_PROFILE, _has_oauth_profile(), SETUP_HELP[Mode.OAUTH_PROFILE]),
        (Mode.KEYRING, has_keyring_key, SETUP_HELP[Mode.KEYRING]),
        (Mode.ENV_KEY, bool(os.environ.get(ENV_API_KEY)), SETUP_HELP[Mode.ENV_KEY]),
        (Mode.AUTH_TOKEN, bool(os.environ.get(ENV_AUTH_TOKEN)), SETUP_HELP[Mode.AUTH_TOKEN]),
        (Mode.BEDROCK, _cloud_backend() is not None
         and _cloud_backend().mode == Mode.BEDROCK, SETUP_HELP[Mode.BEDROCK]),
        (Mode.VERTEX, _cloud_backend() is not None
         and _cloud_backend().mode == Mode.VERTEX, SETUP_HELP[Mode.VERTEX]),
    ]
