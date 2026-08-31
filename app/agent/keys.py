"""Where the Anthropic API key comes from.

Order of preference:

1. The OS keyring (macOS Keychain, Windows Credential Locker, GNOME Keyring /
   KWallet on Linux). This is the recommended place: the secret is stored by
   the operating system, encrypted at rest, and never touches this project.
2. The ``ANTHROPIC_API_KEY`` environment variable, for CI, containers, and
   headless machines where no keyring exists.

The key is deliberately NOT stored in: the SQLite database, source code, any
config file in the repository, browsing history, or bookmarks. Nothing in this
module ever writes the key to disk itself - it hands it to ``keyring`` and
forgets it.

The keyring is optional. Importing ``keyring`` can fail outright on a machine
with a broken backend, so every call here is defensive: a broken keyring
degrades to "use the environment variable", never to a crash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE_NAME = "PyBrowser"
ACCOUNT_NAME = "anthropic-api-key"
ENV_VAR = "ANTHROPIC_API_KEY"


class KeyringUnavailable(RuntimeError):
    """The OS keyring cannot be used on this machine."""


@dataclass(frozen=True)
class KeySource:
    """Where a key came from, for display. Never carries the key itself."""

    available: bool
    origin: str = "none"      # "keyring" | "environment" | "none"
    detail: str = ""


def _guard(exc: BaseException) -> KeyringUnavailable:
    """Convert a keyring failure into KeyringUnavailable, or re-raise.

    Backend detection touches system libraries and can fail in ways that are
    not ``Exception`` at all: a mis-installed ``cryptography`` makes the Rust
    extension panic, and that surfaces as ``pyo3_runtime.PanicException``,
    which derives straight from ``BaseException``. Catching only ``Exception``
    let that take the whole application down when the agent panel was opened -
    so we catch ``BaseException`` here and let only genuine interrupts through.
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc
    return KeyringUnavailable(str(exc) or type(exc).__name__)


def _keyring():
    """Import keyring lazily and defensively."""
    try:
        import keyring as keyring_module

        keyring_module.get_keyring()
        return keyring_module
    except BaseException as exc:  # noqa: BLE001 - see _guard
        raise _guard(exc) from exc


class ApiKeyStore:
    """Reads and writes the API key. Holds nothing in memory beyond a call."""

    def __init__(self, service: str = SERVICE_NAME, account: str = ACCOUNT_NAME) -> None:
        self.service = service
        self.account = account

    # -- reading ---------------------------------------------------------
    def get_keyring_key(self) -> str | None:
        """The key stored in the OS keyring, or None. Never raises.

        Separate from get_key() so credentials.py can decide the precedence
        between the keyring, environment variables and an OAuth profile
        itself, rather than having it baked in here.
        """
        try:
            value = _keyring().get_password(self.service, self.account)
            return value.strip() if value else None
        except KeyringUnavailable:
            return None
        except BaseException as exc:  # noqa: BLE001
            _guard(exc)
            return None

    def get_key(self) -> str | None:
        """Return the key, or None. Prefers the keyring, falls back to env."""
        try:
            value = _keyring().get_password(self.service, self.account)
            if value:
                return value.strip()
        except KeyringUnavailable:
            pass
        except BaseException as exc:  # noqa: BLE001 - a read must never be fatal
            _guard(exc)
        return (os.environ.get(ENV_VAR) or "").strip() or None

    def describe(self) -> KeySource:
        """Report whether a key is configured and where it came from.

        Returns no part of the key - not even a prefix. There is no reason for
        a UI to display any of it.
        """
        try:
            if _keyring().get_password(self.service, self.account):
                return KeySource(True, "keyring", "stored in the OS keyring")
        except KeyringUnavailable as exc:
            if os.environ.get(ENV_VAR):
                return KeySource(True, "environment", f"{ENV_VAR} (keyring unavailable)")
            return KeySource(False, "none", f"no keyring on this system ({exc})")
        except BaseException as exc:  # noqa: BLE001
            _guard(exc)
        if os.environ.get(ENV_VAR):
            return KeySource(True, "environment", f"{ENV_VAR} environment variable")
        return KeySource(False, "none", "no API key configured")

    # -- writing ---------------------------------------------------------
    def set_key(self, key: str) -> None:
        """Store the key in the OS keyring. Raises KeyringUnavailable if it cannot."""
        key = (key or "").strip()
        if not key:
            raise ValueError("The API key is empty.")
        try:
            _keyring().set_password(self.service, self.account, key)
        except KeyringUnavailable:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise _guard(exc) from exc

    def clear_key(self) -> None:
        try:
            _keyring().delete_password(self.service, self.account)
        except BaseException as exc:  # noqa: BLE001 - deleting a missing key is fine
            _guard(exc)

    @staticmethod
    def keyring_available() -> bool:
        """Whether a key can be *stored*. Never raises, for any reason."""
        try:
            _keyring()
            return True
        except KeyringUnavailable:
            return False
        except BaseException as exc:  # noqa: BLE001 - defence in depth
            _guard(exc)
            return False
