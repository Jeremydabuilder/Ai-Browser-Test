"""Sync to a plain folder (Part 7) - the one real, working, zero-server
cross-device story this phase ships. The folder can be a local directory
used for testing, or point at an iCloud Drive/Dropbox/OneDrive-synced
folder for genuine cross-device sync with no PyBrowser-hosted backend at
all: PyBrowser only ever writes encrypted bytes into it, so whatever
already syncs that folder (the OS's own cloud-drive client) is the
transport - this provider never talks to a network itself.

Layout:

    <root>/records/<key>.enc   - one encrypted package per record
    <root>/manifest.json       - key -> {etag, revision} for list_changes

The manifest is small (one JSON object, one line per record) and is
rewritten on every upload - real record content lives only in the
per-record ``.enc`` files, so uploading one changed record never rewrites
another. This is what "sync changed records only" (Part 8) looks like on
disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from app.sync.providers.base import RemoteEntry, SyncProvider, SyncProviderUnavailable


def _safe_filename(key: str) -> str:
    """Keys are always our own generated ids (record_type:global_id), never
    user-controlled path fragments - but treat them as untrusted anyway:
    reject a key that would escape ``records/``. The key's own ":" (and any
    other character Windows reserves in filenames - * ? " < > |) is
    percent-encoded for the on-disk name only; the manifest and every
    caller still use the raw key."""
    if "/" in key or "\\" in key or key in ("", ".", ".."):
        raise ValueError(f"unsafe sync record key: {key!r}")
    return quote(key, safe="") + ".enc"


class LocalFolderProvider(SyncProvider):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._records_dir = self.root / "records"
        self._manifest_path = self.root / "manifest.json"

    def _ensure_dirs(self) -> None:
        try:
            self._records_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot create sync folder: {exc}") from exc

    def _load_manifest(self) -> dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        try:
            return json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SyncProviderUnavailable(f"cannot read sync manifest: {exc}") from exc

    def _save_manifest(self, manifest: dict[str, dict]) -> None:
        try:
            self._manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot write sync manifest: {exc}") from exc

    def upload(self, key: str, data: bytes) -> str:
        self._ensure_dirs()
        filename = _safe_filename(key)
        try:
            (self._records_dir / filename).write_bytes(data)
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot write sync record: {exc}") from exc
        manifest = self._load_manifest()
        revision = max((entry.get("revision", 0) for entry in manifest.values()), default=0) + 1
        etag = str(hash(data) & 0xFFFFFFFF)
        manifest[key] = {"etag": etag, "revision": revision}
        self._save_manifest(manifest)
        return etag

    def download(self, key: str) -> bytes | None:
        path = self._records_dir / _safe_filename(key)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot read sync record: {exc}") from exc

    def list_all(self) -> list[RemoteEntry]:
        manifest = self._load_manifest()
        return [RemoteEntry(key=key, etag=entry.get("etag", ""),
                           modified_at=str(entry.get("revision", 0)))
               for key, entry in manifest.items()]

    def list_changes(self, since_token: str | None) -> tuple[list[RemoteEntry], str]:
        manifest = self._load_manifest()
        cutoff = int(since_token) if since_token else 0
        entries = [(key, entry) for key, entry in manifest.items()
                  if entry.get("revision", 0) > cutoff]
        changed = [RemoteEntry(key=key, etag=entry.get("etag", ""),
                              modified_at=str(entry.get("revision", 0)))
                  for key, entry in entries]
        latest = max((entry.get("revision", 0) for entry in manifest.values()), default=0)
        return changed, str(latest)
