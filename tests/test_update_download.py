"""Update download safety, against a real local HTTP server (no mocks of the
transport): size bounds, cancellation, cleanup, and manifest URL/channel rules."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.updater import checker, manifest as manifest_mod
from app.updater.download import DownloadCancelled, DownloadError, download_artifact
from app.updater.manifest import Artifact, ManifestError
from app.updater.verify import verify_artifact

PAYLOAD = b"x" * 5000


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def do_GET(self):
        body = PAYLOAD
        if self.path.startswith("/long"):
            body = PAYLOAD + b"y" * 5000
        elif self.path.startswith("/short"):
            body = PAYLOAD[:100]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _art(self, path, size=len(PAYLOAD)):
        return Artifact(url=f"{self.base}{path}", sha256=hashlib.sha256(PAYLOAD).hexdigest(), size=size)

    def test_good_download_keeps_exe_suffix_and_verifies(self):
        art = self._art("/PyBrowser-Setup.exe")
        path = download_artifact(art, directory=self.dir)
        self.assertTrue(path.endswith(".exe"))
        self.assertTrue(verify_artifact(path, art).ok)

    def test_oversized_body_rejected_and_no_file_left(self):
        with self.assertRaises(DownloadError):
            download_artifact(self._art("/long/a.exe"), directory=self.dir)
        self.assertEqual(os.listdir(self.dir), [])

    def test_short_body_rejected_and_no_file_left(self):
        with self.assertRaises(DownloadError):
            download_artifact(self._art("/short/a.exe"), directory=self.dir)
        self.assertEqual(os.listdir(self.dir), [])

    def test_cancel_removes_partial_file(self):
        with self.assertRaises(DownloadCancelled):
            download_artifact(self._art("/a.exe"), directory=self.dir, progress=lambda d, t: False)
        self.assertEqual(os.listdir(self.dir), [])

    def test_unreachable_server_is_a_clean_failure(self):
        art = Artifact(url="http://127.0.0.1:9/a.exe", sha256="0" * 64, size=10)
        with self.assertRaises(Exception):
            download_artifact(art, directory=self.dir, timeout=2)
        self.assertEqual(os.listdir(self.dir), [])

    def test_wrong_hash_is_rejected_after_download(self):
        art = Artifact(url=f"{self.base}/a.exe", sha256="0" * 64, size=len(PAYLOAD))
        path = download_artifact(art, directory=self.dir)
        self.assertFalse(verify_artifact(path, art).ok)


class ManifestRuleTests(unittest.TestCase):
    SHA = "a" * 64

    def _m(self, url="https://example.com/a.exe", notes="https://example.com/n", channel="stable"):
        return {"version": "9.0.0", "channel": channel, "notes_url": notes,
                "artifacts": {"windows": {"url": url, "sha256": self.SHA, "size": 1}}}

    def test_plain_http_to_remote_host_rejected(self):
        with self.assertRaises(ManifestError):
            manifest_mod.parse_manifest(self._m(url="http://example.com/a.exe"))

    def test_non_web_schemes_rejected(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x/a.exe"):
            with self.assertRaises(ManifestError):
                manifest_mod.parse_manifest(self._m(notes=bad))
            with self.assertRaises(ManifestError):
                manifest_mod.parse_manifest(self._m(url=bad))

    def test_loopback_http_allowed(self):
        manifest_mod.parse_manifest(self._m(url="http://127.0.0.1:8000/a.exe"))

    def test_preview_manifest_not_offered_to_stable_build(self):
        m = manifest_mod.parse_manifest(self._m(channel="preview"))
        r = checker.evaluate_manifest(m, current="0.1.0", this_platform="windows")
        self.assertFalse(r.available)


if __name__ == "__main__":
    unittest.main()
