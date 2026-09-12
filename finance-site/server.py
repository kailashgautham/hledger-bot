"""Deterministic finance dashboard server.

Reads the hledger journal on each request (cached by mtime) and serves a
static single-page dashboard + a JSON API. Access is guarded by a login page
that issues an HMAC-signed, HttpOnly session cookie (HTTP Basic Auth still
accepted as a fallback for API clients).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from data import build_data, load_settings, settings_path
import workflows

logger = logging.getLogger(__name__)

MAX_UPLOAD = 15 * 1024 * 1024  # 15 MB

PORT = int(os.environ.get("PORT", "8080"))
HLEDGER_PATH = Path(os.environ.get("HLEDGER_PATH", "/finance/journal.hledger"))
SITE_USER = os.environ.get("SITE_USER", "")
SITE_PASS = os.environ.get("SITE_PASS", "")
# Without a stable secret every restart invalidates all sessions, so warn
# rather than silently logging the user out on each deploy.
SITE_SECRET = os.environ.get("SITE_SECRET", "")
if not SITE_SECRET:
    SITE_SECRET = secrets.token_hex(16)
    logging.warning("SITE_SECRET is not set; sessions will not survive a restart")
CURRENCY = os.environ.get("SITE_CURRENCY", "SGD")
SITE_NAME = os.environ.get("SITE_NAME", "")
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
COOKIE_NAME = "finance_session"

if not SITE_USER or not SITE_PASS:
    raise SystemExit("SITE_USER and SITE_PASS must be set")

_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": None}

# Failed-login throttling. The login endpoint is reachable pre-auth from the
# internet, and nothing previously slowed down a password guessing loop.
# Sized for a single-user dashboard: still far too slow to guess a real
# password (10 tries per 5 minutes), but a genuine typo lockout clears while
# you are still looking at the screen rather than a quarter of an hour later.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW = 5 * 60
_login_lock = threading.Lock()
_login_failures: dict = {}


def _login_retry_after(ip: str) -> int:
    """Seconds until this address may try again; 0 when it is not blocked."""
    with _login_lock:
        now = time.time()
        hits = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW]
        _login_failures[ip] = hits
        if len(hits) < LOGIN_MAX_FAILURES:
            return 0
        return max(1, int(LOGIN_WINDOW - (now - min(hits))))


def _record_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures.setdefault(ip, []).append(time.time())
        # Bound the dict so a spray across many source addresses cannot grow it
        # without limit.
        if len(_login_failures) > 2048:
            cutoff = time.time() - LOGIN_WINDOW
            for k in [k for k, v in _login_failures.items() if not any(t > cutoff for t in v)]:
                _login_failures.pop(k, None)


def _clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


def _constant_time_equals(candidate, expected: str) -> bool:
    """compare_digest raises TypeError on non-str or non-ASCII input, and the
    login handler did not guard it — so a JSON body of {"user": 123} produced
    an unhandled exception instead of a 401."""
    if not isinstance(candidate, str):
        return False
    try:
        return hmac.compare_digest(candidate, expected)
    except TypeError:
        # Non-ASCII cannot match an ASCII secret; compare encoded bytes so the
        # timing profile stays flat rather than returning early.
        return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0


def get_data() -> dict:
    # Keyed on the settings file too, so editing budgets.json takes effect on
    # the next request instead of waiting for the journal to change.
    settings_file = settings_path(HLEDGER_PATH)
    key = (_mtime(HLEDGER_PATH), _mtime(settings_file))
    with _lock:
        if _cache["mtime"] != key:
            text = HLEDGER_PATH.read_text() if HLEDGER_PATH.exists() else ""
            _cache["mtime"] = key
            _cache["data"] = build_data(
                text, CURRENCY, load_settings(settings_file), SITE_NAME)
        return _cache["data"]


def authorized(headers) -> bool:
    header = headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, passwd = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return _constant_time_equals(user, SITE_USER) and _constant_time_equals(passwd, SITE_PASS)


def session_token() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(SITE_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def valid_session(cookie: str) -> bool:
    if not cookie:
        return False
    token = None
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            token = v
            break
    if not token:
        return False
    try:
        ts, sig = token.split(".")
        expect = hmac.new(SITE_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return False
        return (time.time() - int(ts)) < SESSION_TTL
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep logs quiet
        pass

    def _authed(self) -> bool:
        return authorized(self.headers) or valid_session(self.headers.get("Cookie", ""))

    def _security_headers(self) -> None:
        """Defence in depth for a publicly reachable finance dashboard."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, loc: str) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_login(self, error: bool = False) -> None:
        body = (Path(__file__).parent / "login.html").read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    def _login(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        ip = self.client_address[0] if self.client_address else "?"
        retry_after = _login_retry_after(ip)
        if retry_after:
            mins = max(1, round(retry_after / 60))
            self.send_response(429)
            body = f"Too many attempts. Try again in about {mins} minute{'' if mins == 1 else 's'}.".encode()
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", str(retry_after))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        user = data.get("user", "")
        passwd = data.get("pass", "")
        if (_constant_time_equals(user, SITE_USER)
                and _constant_time_equals(passwd, SITE_PASS)):
            _clear_login_failures(ip)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={session_token()}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            _record_login_failure(ip)
            self._send(401, b"bad credentials", "text/plain")

    # ------------------------------------------------------------------
    # Request body helpers
    # ------------------------------------------------------------------

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD:
            raise ValueError("Upload too large")
        return self.rfile.read(length) if length else b""

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _json_body(self) -> dict:
        try:
            return json.loads(self._read_body()) if self.headers.get("Content-Length") else {}
        except Exception:
            return {}

    def _json_post(self, fn) -> None:
        data = self._json_body()
        try:
            self._json(fn(data))
        except KeyError:
            self._json({"step": "error", "message": "Session expired or not found. Start again."}, 404)
        except (TypeError, ValueError) as exc:
            self._json({"step": "error", "message": f"Invalid input: {exc}"}, 400)
        except Exception as exc:
            logger.exception("workflow error")
            self._json({"step": "error", "message": f"Internal error: {exc}"}, 500)

    def _parse_multipart(self) -> dict:
        """Minimal multipart/form-data parser for a single file + text fields."""
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", ctype)
        if not m:
            return {"files": {}, "fields": {}}
        boundary = (m.group(1) or m.group(2)).encode()
        body = self._read_body()
        result: dict = {"files": {}, "fields": {}}
        for part in body.split(b"--" + boundary):
            if not part or part in (b"--", b"\r\n", b"\n"):
                continue
            header, _, content = part.partition(b"\r\n\r\n")
            if not header:
                continue
            disp = ""
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-disposition:"):
                    disp = line.decode("latin-1")
                    break
            if "filename=" in disp:
                fm = re.search(r'filename="([^"]*)"', disp)
                name = fm.group(1) if fm else "file"
                if content.endswith(b"\r\n"):
                    content = content[:-2]
                result["files"][name] = content
            else:
                nm = re.search(r'name="([^"]*)"', disp)
                if nm:
                    result["fields"][nm.group(1)] = content.rstrip(b"\r\n").decode("utf-8", "replace")
        return result

    def _import(self) -> None:
        try:
            parsed = self._parse_multipart()
        except ValueError:
            self._json({"step": "error", "message": "Upload too large."}, 413)
            return
        files = parsed.get("files", {})
        if not files:
            self._json({"step": "error", "message": "No file uploaded."}, 400)
            return
        filename, data = next(iter(files.items()))
        if not data:
            self._json({"step": "error", "message": "Empty file."}, 400)
            return
        try:
            self._json(workflows.parse_statement(filename, data))
        except Exception as exc:
            logger.exception("import failed")
            self._json({"step": "error", "message": f"Import failed: {exc}"}, 500)

    def do_GET(self):
        if self.path == "/favicon.svg":
            body = (Path(__file__).parent / "favicon.svg").read_bytes()
            self._send(200, body, "image/svg+xml")
            return
        if self.path == "/apple-touch-icon.png":
            body = (Path(__file__).parent / "apple-touch-icon.png").read_bytes()
            self._send(200, body, "image/png")
            return
        if self.path == "/manifest.json":
            # Served unauthenticated: iOS fetches it before any session exists,
            # and it contains nothing private.
            body = (Path(__file__).parent / "manifest.json").read_bytes()
            self._send(200, body, "application/manifest+json")
            return
        if self.path == "/login" or self.path.startswith("/login?"):
            if self._authed():
                self._redirect("/")
            else:
                self._serve_login()
            return
        if not self._authed():
            self._redirect("/login")
            return
        if self.path == "/api/data" or self.path.startswith("/api/data?"):
            payload = json.dumps(get_data()).encode()
            self._send(200, payload, "application/json")
            return
        if self.path == "/api/accounts":
            self._json({"accounts": workflows.accounts_list()})
            return
        if self.path == "/api/reconcile":
            self._json(workflows.reconcile_accounts())
            return
        if self.path == "/api/last-change":
            self._json(workflows.last_change())
            return
        if self.path in ("/", "/index.html"):
            body = (Path(__file__).parent / "index.html").read_bytes()
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/login":
            self._login()
            return
        if not self._authed():
            self._send(401, b"unauthorized", "text/plain")
            return
        if self.path == "/api/import":
            self._import()
            return
        if self.path == "/api/import/start":
            self._json_post(lambda d: workflows.start_categorisation(
                d.get("wizard_id"), d.get("card_name"), d.get("offset_account")))
            return
        if self.path == "/api/import/tx":
            self._json_post(lambda d: workflows.tx_action(
                d.get("wizard_id"), d.get("action"), d))
            return
        if self.path == "/api/reconcile":
            self._json_post(lambda d: workflows.reconcile_submit(
                d.get("account"), float(d.get("actual"))))
            return
        if self.path == "/api/reconcile/settle":
            self._json_post(lambda d: workflows.reconcile_settle(
                d.get("account"), float(d.get("actual")), float(d.get("diff"))))
            return
        if self.path == "/api/txn/edit":
            self._json_post(lambda d: workflows.tx_edit(d))
            return
        if self.path == "/api/txn/add":
            self._json_post(lambda d: workflows.tx_add(d))
            return
        if self.path == "/api/txn/delete":
            self._json_post(lambda d: workflows.tx_delete(d))
            return
        if self.path == "/api/txn/amortize":
            self._json_post(lambda d: workflows.tx_amortize(d))
            return
        if self.path == "/api/undo":
            self._json_post(lambda d: workflows.undo_last_change())
            return
        self._send(404, b"not found", "text/plain")


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
