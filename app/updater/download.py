"""Downloading an update artifact safely.

The manifest says how big the file must be, so the download is bounded by
that size: a server that keeps sending bytes cannot fill the disk, and a
short or long body is rejected before it is hashed. The file keeps the
extension of the URL's file name - Windows will not launch an installer
that has no ``.exe`` - and a failed or cancelled download never leaves a
partial file behind.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from app.updater.manifest import Artifact

_ALLOWED_SUFFIXES = (".exe", ".msi", ".dmg", ".pkg", ".zip")


class DownloadError(Exception):
    """The download did not produce a complete file of the expected size."""


class DownloadCancelled(DownloadError):
    pass


def _suffix_for(url: str) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix if suffix in _ALLOWED_SUFFIXES else ""


def download_artifact(artifact: Artifact, *, directory: str | None = None,
                      progress: Callable[[int, int], bool | None] | None = None,
                      timeout: float = 60.0) -> str:
    """Download ``artifact`` and return the local path. ``progress(done,
    total)`` is called per chunk; returning ``False`` cancels. The result is
    size-checked here and hash-checked by ``verify_artifact`` afterwards."""
    import httpx2 as httpx

    fd, path = tempfile.mkstemp(prefix="pybrowser-update-", suffix=_suffix_for(artifact.url),
                                dir=directory)
    os.close(fd)
    try:
        done = 0
        with httpx.stream("GET", artifact.url, follow_redirects=True, timeout=timeout) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > artifact.size:
                raise DownloadError(
                    f"the server announced {declared} bytes but the release lists {artifact.size}")
            with open(path, "wb") as fh:
                for chunk in response.iter_bytes():
                    done += len(chunk)
                    if done > artifact.size:
                        raise DownloadError(
                            f"the download is larger than the {artifact.size} bytes the release lists")
                    fh.write(chunk)
                    if progress is not None and progress(done, artifact.size) is False:
                        raise DownloadCancelled("download cancelled")
        if done != artifact.size:
            raise DownloadError(
                f"the download ended after {done} of {artifact.size} bytes")
        return path
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
