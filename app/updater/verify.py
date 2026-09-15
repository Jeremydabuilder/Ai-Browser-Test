"""Phase 22 Part 4: artifact verification. Before offering to run/open a
downloaded installer, its SHA-256 (and, incidentally, its size) must
match the release manifest's own record for it - a mismatch is a hard
rejection, never a warning the user can click past, since this is the
one check standing between "downloaded from a URL the manifest named"
and "actually the bytes we said it would be."
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.updater.manifest import Artifact

_CHUNK_SIZE = 1024 * 1024


def sha256_of_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str = ""


def verify_artifact(path: str | Path, artifact: Artifact) -> VerificationResult:
    """Check a downloaded file against its manifest record. Rejects on
    size mismatch first (cheap, catches a truncated/wrong download
    immediately) before spending time hashing the whole file."""
    path = Path(path)
    if not path.is_file():
        return VerificationResult(ok=False, reason=f"file not found: {path}")
    actual_size = path.stat().st_size
    if actual_size != artifact.size:
        return VerificationResult(
            ok=False,
            reason=f"size mismatch: expected {artifact.size} bytes, got {actual_size}")
    actual_sha256 = sha256_of_file(path)
    if actual_sha256.lower() != artifact.sha256.lower():
        return VerificationResult(
            ok=False,
            reason=f"checksum mismatch: expected {artifact.sha256}, got {actual_sha256}")
    return VerificationResult(ok=True)
