"""Deterministic finance dashboard server.

Reads the hledger journal on each request (cached by mtime) and serves a
static single-page dashboard + a JSON API. HTTP Basic Auth guards everything.
"""
import base64
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from data import build_data

PORT = int(os.environ.get("PORT", "8080"))
HLEDGER_PATH = Path(os.environ.get("HLEDGER_PATH", "/finance/journal.hledger"))
SITE_USER = os.environ.get("SITE_USER", "")
SITE_PASS = os.environ.get("SITE_PASS", "")
CURRENCY = os.environ.get("SITE_CURRENCY", "SGD")

if not SITE_USER or not SITE_PASS:
    raise SystemExit("SITE_USER and SITE_PASS must be set")

_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": None}


def get_data() -> dict:
    mtime = HLEDGER_PATH.stat().st_mtime if HLEDGER_PATH.exists() else 0
    with _lock:
        if _cache["mtime"] != mtime:
            text = HLEDGER_PATH.read_text() if HLEDGER_PATH.exists() else ""
            _cache["mtime"] = mtime
            _cache["data"] = build_data(text, CURRENCY)
        return _cache["data"]


def authorized(headers) -> bool:
    header = headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, passwd = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, SITE_USER) and hmac.compare_digest(passwd, SITE_PASS)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep logs quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Finance"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not authorized(self.headers):
            self._unauthorized()
            return
        if self.path == "/api/data" or self.path.startswith("/api/data?"):
            payload = json.dumps(get_data()).encode()
            self._send(200, payload, "application/json")
            return
        if self.path in ("/", "/index.html"):
            body = (Path(__file__).parent / "index.html").read_bytes()
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    do_POST = do_GET


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
