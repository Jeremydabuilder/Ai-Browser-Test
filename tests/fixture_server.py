"""A deterministic local web server for BrowserController tests.

Every page here is fixed and self-contained, so the tests never depend on an
external site being up, unblocked, or unchanged. The pages deliberately cover
the behaviours that make real automation hard: content that appears late,
elements created by script, nodes that are recycled for different content,
navigation triggered from JavaScript, redirects and forms.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture Home</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; color: #123; }
 .tall { height: 2400px; }
 .hidden-box { display: none; }
</style></head>
<body>
  <h1>Fixture Home</h1>
  <h2>Controls</h2>

  <a id="internal" href="/second">Second page</a>
  <a id="blank" href="/second" target="_blank">Open in new tab</a>
  <a id="redirect" href="/redirect">Redirecting link</a>
  <a id="download" href="/files/installer.exe">Download installer</a>

  <button id="counter" type="button">Clicked 0 times</button>
  <button id="adder" type="button">Add a button</button>
  <button id="reveal" type="button">Reveal hidden</button>
  <button id="jsnav" type="button">Go via JavaScript</button>
  <button id="disabled-btn" type="button" disabled>Disabled button</button>
  <button id="recycle" type="button">Recycle label</button>
  <button id="remover" type="button">Remove the target</button>
  <button id="victim" type="button">Removable target</button>
  <button id="buy" type="button">Buy now</button>

  <div class="hidden-box" id="secret"><button id="hidden-btn" type="button">Hidden button</button></div>

  <form id="search-form" action="/results" method="get">
    <label for="q">Search terms</label>
    <input id="q" name="q" type="search" placeholder="Search the fixtures">
    <label for="pw">Password</label>
    <input id="pw" name="password" type="password" autocomplete="current-password">
    <textarea id="notes" name="notes" placeholder="Notes"></textarea>
    <select id="colour" name="colour">
      <option value="red">Red</option>
      <option value="green" selected>Green</option>
      <option value="blue">Blue</option>
    </select>
    <input id="agree" name="agree" type="checkbox"> <label for="agree">I agree</label>
    <input id="r1" name="size" type="radio" value="s"> <label for="r1">Small</label>
    <input id="r2" name="size" type="radio" value="l"> <label for="r2">Large</label>
    <button id="submit-btn" type="submit">Search</button>
  </form>

  <div id="late"></div>
  <div class="tall">scroll space</div>

<script>
  var clicks = 0;
  document.getElementById('counter').onclick = function () {
    clicks++; this.textContent = 'Clicked ' + clicks + ' times';
  };
  document.getElementById('adder').onclick = function () {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = 'Generated button ' + (document.querySelectorAll('.generated').length + 1);
    b.className = 'generated';
    document.body.appendChild(b);
    var a = document.createElement('a');
    a.href = '/second';
    a.textContent = 'Generated link';
    a.className = 'generated';
    document.body.appendChild(a);
    var i = document.createElement('input');
    i.type = 'text';
    i.placeholder = 'Generated input';
    i.id = 'generated-input';
    document.body.appendChild(i);
  };
  document.getElementById('reveal').onclick = function () {
    document.getElementById('secret').style.display = 'block';
  };
  document.getElementById('jsnav').onclick = function () { location.href = '/second'; };
  // Recycles an existing node for entirely different content - the case that
  // makes naive element references click the wrong thing.
  document.getElementById('recycle').onclick = function () {
    var v = document.getElementById('victim');
    v.textContent = 'Completely different action';
  };
  document.getElementById('remover').onclick = function () {
    var v = document.getElementById('victim');
    if (v) { v.remove(); }
  };
  // Content that only shows up well after the load event.
  setTimeout(function () {
    var d = document.createElement('div');
    d.id = 'delayed';
    d.textContent = 'Delayed content has arrived';
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = 'Delayed button';
    d.appendChild(b);
    document.getElementById('late').appendChild(d);
  }, 700);
</script>
</body></html>"""

SECOND = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Second Page</title></head>
<body><h1>Second page</h1><a id="home" href="/">Home</a></body></html>"""

RESULTS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Results</title></head>
<body><h1>Results</h1><p id="query">query=%s</p><a href="/">Home</a></body></html>"""

REDIRECT_TARGET = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Redirect Target</title></head>
<body><h1>Arrived after a redirect</h1></body></html>"""

SLOW = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Slow Page</title></head>
<body><h1>Slow page finished</h1></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index":
            self._send(INDEX)
        elif path == "/second":
            self._send(SECOND)
        elif path == "/results":
            query = parse_qs(parsed.query).get("q", [""])[0]
            self._send(RESULTS % query.replace("<", "&lt;"))
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/redirected":
            self._send(REDIRECT_TARGET)
        elif path == "/slow":
            # Deterministically slow: the response body is delayed server-side.
            import time as _time
            _time.sleep(0.6)
            self._send(SLOW)
        elif path.startswith("/files/"):
            self._send("binary-stand-in", content_type="application/octet-stream")
        else:
            self._send("<!doctype html><title>Not Found</title><h1>404</h1>", status=404)

    def log_message(self, *args) -> None:  # silence request logging
        return


class FixtureServer:
    """Starts on an ephemeral port; ``base`` is the URL to hand the browser."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def url(self, path: str) -> str:
        return self.base.rstrip("/") + "/" + path.lstrip("/")

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
