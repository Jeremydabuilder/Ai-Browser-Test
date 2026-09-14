"""An in-memory SyncProvider - the fast path for unit tests (Part 25:
"CI must not depend on a cloud service"). Two InMemoryProvider instances
can share the same SharedMemoryBacking to simulate two devices syncing
through the same remote storage without touching a filesystem at all.

Revisions are a plain incrementing counter, not a wall-clock timestamp -
deterministic even when a test uploads many records within the same
microsecond, which a time.time()-based token could not tell apart.
"""

from __future__ import annotations

from app.sync.providers.base import RemoteEntry, SyncProvider, SyncProviderUnavailable


class SharedMemoryBacking:
    """Pass the same instance to two InMemoryProvider constructions to
    simulate two devices syncing through one shared remote."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.meta: dict[str, tuple[str, int]] = {}  # key -> (etag, revision)
        self.revision = 0


class InMemoryProvider(SyncProvider):
    def __init__(self, backing: "SharedMemoryBacking | None" = None) -> None:
        self._backing = backing if backing is not None else SharedMemoryBacking()
        self._unavailable = False

    def set_unavailable(self, unavailable: bool) -> None:
        """Part 19/25: simulate the provider going offline."""
        self._unavailable = unavailable

    def _check(self) -> None:
        if self._unavailable:
            raise SyncProviderUnavailable("in-memory provider is simulating being offline")

    def upload(self, key: str, data: bytes) -> str:
        self._check()
        self._backing.data[key] = data
        self._backing.revision += 1
        etag = str(hash(data) & 0xFFFFFFFF)
        self._backing.meta[key] = (etag, self._backing.revision)
        return etag

    def download(self, key: str) -> bytes | None:
        self._check()
        return self._backing.data.get(key)

    def list_all(self) -> list[RemoteEntry]:
        self._check()
        return [RemoteEntry(key=key, etag=etag, modified_at=str(revision))
               for key, (etag, revision) in self._backing.meta.items()]

    def list_changes(self, since_token: str | None) -> tuple[list[RemoteEntry], str]:
        self._check()
        cutoff = int(since_token) if since_token else 0
        changed = [RemoteEntry(key=key, etag=etag, modified_at=str(revision))
                  for key, (etag, revision) in self._backing.meta.items()
                  if revision > cutoff]
        return changed, str(self._backing.revision)
