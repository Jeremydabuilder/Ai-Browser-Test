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
import os
from pathlib import Path
from urllib.parse import quote, unquote

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

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        """Write to a temp file in the same folder, then rename over the
        target, so a crash, a full disk or a cloud client that picks the
        file up mid-write sees either the old complete file or the new
        complete one - never a truncated record."""
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _scan(self) -> dict[str, str]:
        """key -> fingerprint, straight from the record files on disk. The
        files are the truth; the manifest is only an index over them."""
        found: dict[str, str] = {}
        if not self._records_dir.is_dir():
            return found
        try:
            for path in self._records_dir.iterdir():
                if path.suffix != ".enc" or path.name.startswith("."):
                    continue
                stat = path.stat()
                found[unquote(path.stem)] = f"{stat.st_size}-{stat.st_mtime_ns}"
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot read sync folder: {exc}") from exc
        return found

    def _load_manifest(self) -> dict[str, dict]:
        """The index, reconciled with the files actually present.

        Two devices writing the same shared folder can overwrite each
        other's manifest (it is one file, and a cloud drive offers no lock),
        and a damaged manifest must not wedge sync for good. So a record
        file the manifest does not know - or knows with a different
        fingerprint - is given a fresh revision, which makes every peer pull
        it again (pulling is idempotent), and entries whose file is gone are
        dropped."""
        manifest: dict[str, dict] = {}
        if self._manifest_path.exists():
            try:
                loaded = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = {k: v for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                manifest = {}  # rebuilt below from the files themselves
        files = self._scan()
        revision = max((e.get("revision", 0) for e in manifest.values()), default=0)
        changed = False
        for key in [k for k in manifest if k not in files]:
            del manifest[key]
            changed = True
        for key, fingerprint in files.items():
            entry = manifest.get(key)
            if entry is None or entry.get("etag") != fingerprint:
                revision += 1
                manifest[key] = {"etag": fingerprint, "revision": revision}
                changed = True
        if changed:
            try:
                self._save_manifest(manifest)
            except SyncProviderUnavailable:
                pass  # read-only folder: still usable for pulling
        return manifest

    def _save_manifest(self, manifest: dict[str, dict]) -> None:
        try:
            self._write_atomic(self._manifest_path, json.dumps(manifest).encode("utf-8"))
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot write sync manifest: {exc}") from exc

    def upload(self, key: str, data: bytes) -> str:
        self._ensure_dirs()
        path = self._records_dir / _safe_filename(key)
        try:
            self._write_atomic(path, data)
        except OSError as exc:
            raise SyncProviderUnavailable(f"cannot write sync record: {exc}") from exc
        # _load_manifest notices the new fingerprint and assigns the next
        # revision, so there is one code path for "this file changed".
        manifest = self._load_manifest()
        return manifest.get(key, {}).get("etag", "")

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
