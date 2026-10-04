"""Page fetcher over REAL sockets (no HTTP mocks): proves where the connection
actually goes, not just which address was checked.

A local HTTP server stands in for "the public internet". ``socket.create_connection``
(the single place the HTTP stack connects) is wrapped: it records every
destination and redirects only the pinned public test address to the local server;
anything else would fail. So a test can assert exactly which addresses were dialled.

Run with:  python tests/run.py tests.test_team_webfetch_network
"""

from __future__ import annotations

import gzip
import os
import socket
import sys
import threading
import tracemalloc
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.team import webfetch as wf  # noqa: E402

PUBLIC = "93.184.216.34"          # a globally routable address (never actually dialled)
PAGE = b"<html><head><title>T</title></head><body><article><p>" + b"Real article text. " * 10 + b"</p></article></body></html>"

_real_create_connection = socket.create_connection


class Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen_hosts: list = []

    def log_message(self, *a) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_hosts.append(self.headers.get("Host"))
        route = type(self).routes.get(self.path)
        if route is None:
            self.send_response(404); self.end_headers(); return
        route(self)


def send(handler, body=PAGE, status=200, headers=None):
    handler.send_response(status)
    for k, v in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                 **(headers or {})}.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


class Resolver:
    def __init__(self, answers):
        self.answers = answers          # host -> list of lists (one per call; last repeats)
        self.calls: list[str] = []

    def __call__(self, host, port):
        self.calls.append(host)
        seq = self.answers[host]
        index = min(len([c for c in self.calls if c == host]) - 1, len(seq) - 1)
        return list(seq[index])


class RealSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        Handler.routes = {"/": send}
        Handler.seen_hosts = []
        self.dialled: list[tuple] = []
        outer = self

        def create_connection(address, timeout=None, source_address=None, **kw):
            outer.dialled.append(tuple(address))
            if address[0] == PUBLIC:
                return _real_create_connection(("127.0.0.1", outer.port), timeout)
            raise ConnectionRefusedError(f"test blocks {address}")

        patch = mock.patch.object(socket, "create_connection", create_connection)
        patch.start()
        self.addCleanup(patch.stop)

    def fetcher(self, resolver, **kw):
        return wf.PageFetcher(resolver=resolver, **kw)

    def test_the_connection_goes_to_the_validated_address_not_the_hostname(self) -> None:
        resolver = Resolver({"news.example": [[PUBLIC]]})
        page = self.fetcher(resolver).fetch("http://news.example/")
        self.assertIn("Real article text", page.text)
        self.assertEqual(self.dialled, [(PUBLIC, 80)])           # an IP literal: no second DNS lookup possible
        self.assertEqual(Handler.seen_hosts, ["news.example"])   # the site still sees its own name
        self.assertEqual(resolver.calls, ["news.example"])

    def test_dns_rebinding_to_loopback_after_the_check_cannot_be_dialled(self) -> None:
        # First answer is public, every later answer is loopback - the classic rebinding trick.
        resolver = Resolver({"evil.example": [[PUBLIC], ["127.0.0.1"]]})
        Handler.routes = {"/": lambda h: send(h, b"", 302, {"Location": "/second", "Content-Length": "0"}),
                          "/second": send}
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetcher(resolver).fetch("http://evil.example/")
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.BLOCKED)
        self.assertEqual(self.dialled, [(PUBLIC, 80)])           # the hop to 127.0.0.1 was never attempted

    def test_mixed_public_and_private_answers_are_refused_before_any_connection(self) -> None:
        resolver = Resolver({"mixed.example": [[PUBLIC, "10.0.0.5"]]})
        with self.assertRaises(wf.FetchError):
            self.fetcher(resolver).fetch("http://mixed.example/")
        self.assertEqual(self.dialled, [])

    def test_redirects_to_internal_destinations_are_never_dialled(self) -> None:
        for target in ("http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/",
                       "http://localhost/", "http://[::1]/", "http://2130706433/", "file:///etc/passwd",
                       "http://internal.corp/"):
            with self.subTest(target=target):
                Handler.routes = {"/": lambda h, t=target: send(h, b"", 302, {"Location": t, "Content-Length": "0"})}
                self.dialled.clear()
                with self.assertRaises(wf.FetchError):
                    self.fetcher(Resolver({"r.example": [[PUBLIC]]})).fetch("http://r.example/")
                self.assertEqual(self.dialled, [(PUBLIC, 80)])

    def test_a_redirect_to_a_hostname_that_resolves_privately_is_blocked(self) -> None:
        resolver = Resolver({"a.example": [[PUBLIC]], "b.example": [["192.168.1.10"]]})
        Handler.routes = {"/": lambda h: send(h, b"", 301, {"Location": "http://b.example/", "Content-Length": "0"})}
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetcher(resolver).fetch("http://a.example/")
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.BLOCKED)
        self.assertEqual(self.dialled, [(PUBLIC, 80)])

    def test_proxy_environment_variables_are_ignored(self) -> None:
        env = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
               "ALL_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
        with mock.patch.dict(os.environ, env):
            self.fetcher(Resolver({"p.example": [[PUBLIC]]})).fetch("http://p.example/")
        self.assertEqual(self.dialled, [(PUBLIC, 80)])           # never the proxy: validation covers the real hop

    def test_a_gzip_bomb_is_bounded_in_memory_and_in_output(self) -> None:
        bomb = gzip.compress(b"<html><body><article><p>" + b"a " * 60_000_000 + b"</p></article></body></html>", 1)
        self.assertLess(len(bomb), 2_000_000)
        Handler.routes = {"/": lambda h: send(h, bomb, headers={"Content-Encoding": "gzip"})}
        tracemalloc.start()
        try:
            page = self.fetcher(Resolver({"z.example": [[PUBLIC]]}), max_bytes=200_000, max_chars=5_000).fetch(
                "http://z.example/")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(page.truncated)
        self.assertLessEqual(len(page.text), 5_100)
        self.assertLess(peak, 12_000_000, f"decoding used {peak:,} bytes")      # 120 MB of output was never held

    def test_an_unsupported_content_encoding_is_refused(self) -> None:
        Handler.routes = {"/": lambda h: send(h, b"xx", headers={"Content-Encoding": "br"})}
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetcher(Resolver({"b.example": [[PUBLIC]]})).fetch("http://b.example/")
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.UNSUPPORTED)

    def test_internationalised_and_percent_encoded_hosts_are_normalised_or_refused(self) -> None:
        self.assertEqual(wf.validate_url("http://b\u00fccher.example/x")[1], "xn--bcher-kva.example")
        for bad in ("http://%31%32%37.0.0.1/", "http://b\u00fccher\u202e.example/"):
            with self.assertRaises(wf.FetchError):
                wf.validate_url(bad)
        resolver = Resolver({"xn--bcher-kva.example": [[PUBLIC]]})
        self.fetcher(resolver).fetch("http://b\u00fccher.example/")
        self.assertEqual(Handler.seen_hosts, ["xn--bcher-kva.example"])

    def test_cookies_set_by_a_site_are_not_replayed(self) -> None:
        sent: list = []

        def route(h):
            sent.append(h.headers.get("Cookie"))
            send(h, headers={"Set-Cookie": "sid=1"})
        Handler.routes = {"/": route}
        fetcher = self.fetcher(Resolver({"c.example": [[PUBLIC]]}))
        fetcher.fetch("http://c.example/")
        fetcher.fetch("http://c.example/")
        self.assertEqual(sent, [None, None])


if __name__ == "__main__":
    unittest.main()
