"""The sync provider interface (Part 6). A provider is dumb, opaque
storage: it lists, uploads, and downloads encrypted blobs keyed by a
record's global id - it never sees plaintext, never makes a merge
decision, and never knows what "a Mission" or "a Skill" is. Every method
here must be safe to call when the provider is unreachable (Part 19 -
offline is a normal state, not an error): implementations raise
``SyncProviderUnavailable`` for a transient failure so the engine can
queue changes and retry later, and anything else is a genuine bug.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class SyncProviderUnavailable(Exception):
    """The provider could not be reached right now (network down, folder
    unmounted, etc). Never raised for "the key/record does not exist" -
    that is a normal, expected outcome (see download() returning None)."""


@dataclass(frozen=True)
class RemoteEntry:
    """One stored blob's metadata, as the provider knows it - never its
    decoded content, which the provider cannot read."""

    key: str
    etag: str
    modified_at: str


class SyncProvider(ABC):
    """Storage transport only. See LocalFolderProvider/InMemoryProvider
    for the two implementations this phase ships, and the module docstring
    in local_folder.py for why a plain synced folder is a complete,
    real cross-device story without building a server.
    """

    @abstractmethod
    def list_changes(self, since_token: str | None) -> tuple[list[RemoteEntry], str]:
        """Every stored key whose metadata changed since ``since_token``
        (None means "everything"), plus a new token to pass next time.
        Must not require downloading every blob's content to answer this -
        that is exactly the "do not upload/download a full database every
        sync" cost Part 8 asks to avoid."""

    @abstractmethod
    def upload(self, key: str, data: bytes) -> str:
        """Store ``data`` (an encrypted package's bytes) under ``key``,
        returning its new etag."""

    @abstractmethod
    def download(self, key: str) -> bytes | None:
        """The stored bytes for ``key``, or None if nothing is stored
        there (never raises for a missing key - only for a genuine
        transport failure)."""

    @abstractmethod
    def list_all(self) -> list[RemoteEntry]:
        """Every key currently stored - used for a first sync (no prior
        token) and for tests; real incremental syncs prefer list_changes."""
