"""HTTPS certificate validation of the page fetcher against a LOCAL TLS server.

The fetcher connects to the already-checked IP address but must verify the
certificate against (and send SNI for) the ORIGINAL hostname. A tiny CA issues
certificates on the fly; the client trusts that CA only in the positive case.

Verified here: trusted+valid succeeds; untrusted CA, expired, wrong-host and
"valid for the IP but not the name" all fail; SNI and Host carry the hostname;
the dialled address is the pinned IP. NOT verified: the real public CA store,
real internet servers, Windows' certificate store.

Run with:  python tests/run.py tests.test_team_webfetch_tls
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:                                      # pragma: no cover
    x509 = None

from app.team import webfetch as wf  # noqa: E402

PUBLIC = "93.184.216.34"
PAGE = (b"<html><head><title>Secure</title></head><body><article><p>"
        + b"Secure article text. " * 10 + b"</p></article></body></html>")
_real_create_connection = socket.create_connection


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn="Test CA"):
    key = _key()
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def issue(ca, names=(), ips=(), *, expired=False):
    ca_key, ca_cert = ca
    key = _key()
    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = ((now - datetime.timedelta(days=30), now - datetime.timedelta(days=1)) if expired
                  else (now - datetime.timedelta(days=1), now + datetime.timedelta(days=30)))
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(_name(names[0] if names else "leaf")).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def write_pem(folder, name, key, cert):
    cert_path, key_path = os.path.join(folder, name + ".crt"), os.path.join(folder, name + ".key")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    return cert_path, key_path


class Handler(BaseHTTPRequestHandler):
    hosts: list = []

    def log_message(self, *a) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).hosts.append(self.headers.get("Host"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


class Resolver:
    def __call__(self, host, port):
        return [PUBLIC]


@unittest.skipIf(x509 is None, "the 'cryptography' package is not installed")
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.mkdtemp(prefix="pybrowser-tls-")
        cls.ca = make_ca()
        cls.other_ca = make_ca("Some Other CA")
        cls.servers: list = []

    @classmethod
    def tearDownClass(cls) -> None:
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def serve(self, key_cert):
        """TLS server presenting ``key_cert``; returns (port, sni_log)."""
        cert_path, key_path = write_pem(self.dir, f"s{len(self.servers)}", *key_cert)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        sni: list = []
        context.sni_callback = lambda sock, name, ctx: sni.append(name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1], sni

    def setUp(self) -> None:
        Handler.hosts = []
        self.dialled: list = []
        self.port = 0
        outer = self

        def create_connection(address, timeout=None, source_address=None, **kw):
            outer.dialled.append(tuple(address))
            if address[0] == PUBLIC:
                return _real_create_connection(("127.0.0.1", outer.port), timeout)
            raise ConnectionRefusedError(f"blocked in test: {address}")

        patcher = mock.patch.object(socket, "create_connection", create_connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def trusting(self, ca):
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=ca[1].public_bytes(serialization.Encoding.PEM).decode())
        return context

    def fetch(self, key_cert, *, trust=None, url="https://secure.example/"):
        self.port, self.sni = self.serve(key_cert)
        fetcher = wf.PageFetcher(resolver=Resolver(), verify=self.trusting(trust or self.ca))
        return fetcher.fetch(url)

    def test_a_valid_certificate_for_the_hostname_succeeds_via_the_pinned_ip(self) -> None:
        page = self.fetch(issue(self.ca, ["secure.example"]))
        self.assertIn("Secure article text", page.text)
        self.assertEqual(self.dialled, [(PUBLIC, 443)])          # connected to the checked IP ...
        self.assertEqual(self.sni, ["secure.example"])            # ... but spoke to it as the hostname
        self.assertEqual(Handler.hosts, ["secure.example"])

    def test_a_certificate_from_an_untrusted_authority_is_refused(self) -> None:
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetch(issue(self.other_ca, ["secure.example"]))
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.TLS)

    def test_an_expired_certificate_is_refused(self) -> None:
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetch(issue(self.ca, ["secure.example"], expired=True))
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.TLS)

    def test_a_certificate_for_another_hostname_is_refused(self) -> None:
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetch(issue(self.ca, ["other.example"]))
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.TLS)

    def test_a_certificate_valid_only_for_the_ip_address_is_refused(self) -> None:
        # Proves the name checked is the original hostname, not the IP we dialled.
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetch(issue(self.ca, [], [PUBLIC]))
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.TLS)

    def test_the_default_trust_store_rejects_the_test_authority(self) -> None:
        self.port, _ = self.serve(issue(self.ca, ["secure.example"]))
        with self.assertRaises(wf.FetchError) as ctx:                 # verify=True: system/certifi store only
            wf.PageFetcher(resolver=Resolver()).fetch("https://secure.example/")
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.TLS)

    def test_a_failed_handshake_is_reported_without_leaking_page_content(self) -> None:
        with self.assertRaises(wf.FetchError) as ctx:
            self.fetch(issue(self.ca, ["other.example"]))
        self.assertNotIn("Secure article", ctx.exception.message)
        self.assertIn("certificate", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
