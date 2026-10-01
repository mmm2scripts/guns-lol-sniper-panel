#!/usr/bin/env python3
"""
Guns.lol User Sniper - control-panel server.

Wraps the existing 67.py in a small authenticated JSON API and serves the mobile dashboard (web/).
Pure standard library: the only third-party packages are the ones 67.py already needs (requests, colorama).

    python server.py              start the panel (generates an API key on first run)
    python server.py --new-key    create a new API key (invalidates the old one)
"""
import hashlib
import hmac
import json
import os
import secrets
import signal
import ssl
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from panel.config_store import ConfigStore, ConnectionTester
from panel.supervisor import Supervisor

VERSION = "1.0.0"
ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Tiny .env reader (KEY=VALUE). Real environment variables always win."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv(os.path.join(ROOT, ".env"))
DATA_DIR = os.environ.get("PANEL_DATA_DIR", os.path.join(ROOT, "data"))
WEB_DIR = os.path.join(ROOT, "web")
KEY_HASH_FILE = os.path.join(DATA_DIR, "api_key.sha256")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
TLS_CERT, TLS_KEY = os.environ.get("TLS_CERT"), os.environ.get("TLS_KEY")
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")     # auth is a bearer token (no cookies), so "*" is safe
AUTO_RESUME = os.environ.get("AUTO_RESUME", "1") != "0"
MAX_BODY = 256 * 1024
STARTED = time.time()

# Only these files are ever served without authentication (they contain no secrets).
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}

# ---------------------------------------------------------------------- API key handling
def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    key = secrets.token_urlsafe(32)
    with open(KEY_HASH_FILE, "w") as f:
        f.write(_hash(key))
    try:
        os.chmod(KEY_HASH_FILE, 0o600)
    except OSError:
        pass
    return key


def load_key_hash():
    env = os.environ.get("PANEL_API_KEY", "").strip()
    if env:
        if len(env) < 24:
            sys.exit("PANEL_API_KEY must be at least 24 characters. Generate one with: python server.py --new-key")
        return _hash(env), None
    try:
        with open(KEY_HASH_FILE) as f:
            h = f.read().strip()
        if len(h) == 64:
            return h, None
    except OSError:
        pass
    key = new_key_file()
    with open(KEY_HASH_FILE) as f:
        return f.read().strip(), key


class AuthThrottle:
    """Lock a client address out for a while after repeated bad keys."""
    def __init__(self, limit=8, window=300):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, ip):
        with self.lock:
            t = time.time()
            self.fails[ip] = [x for x in self.fails.get(ip, []) if t - x < self.window]
            return len(self.fails[ip]) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


KEY_HASH, FIRST_RUN_KEY = None, None
THROTTLE = AuthThrottle()
STORE = ConfigStore(ROOT)
SUP = Supervisor(ROOT, DATA_DIR, secrets_fn=lambda: STORE.secret_values() + [os.environ.get("PANEL_API_KEY", "")])
TESTER = ConnectionTester(ROOT, STORE)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GunsPanel"
    sys_version = ""

    def log_message(self, fmt, *args):      # no access log: keeps URLs/headers out of stdout
        pass

    # ------------------------------------------------------------ helpers
    def client_ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _headers(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        if TLS_CERT:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def json(self, data, code=200, extra=None):
        body = json.dumps(data, separators=(",", ":")).encode()
        h = {"Cache-Control": "no-store"}
        h.update(extra or {})
        self._headers(code, "application/json; charset=utf-8", len(body), h)
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, code, msg, extra=None):
        self.json({"error": msg}, code, extra)

    def drain_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            self.rfile.read(n)

    def body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.error(413, "Request body too large")
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            self.error(400, "Body must be valid JSON")
            return None
        return data

    def authed(self):
        ip = self.client_ip()
        if THROTTLE.blocked(ip):
            self.error(429, "Too many failed attempts - try again in a few minutes", {"Retry-After": "300"})
            return False
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token and hmac.compare_digest(_hash(token), KEY_HASH):
            THROTTLE.ok(ip)
            return True
        THROTTLE.fail(ip)
        self.error(401, "Missing or invalid API key", {"WWW-Authenticate": "Bearer"})
        return False

    # ------------------------------------------------------------ verbs
    def do_OPTIONS(self):
        self._headers(204, "text/plain", 0)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self.error(405, "Method not allowed")

    def _route(self, method):
        try:
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if not path.startswith("/api/"):
                if method == "GET" or self.command == "HEAD":
                    return self.static(path)
                return self.error(405, "Method not allowed")
            if not self.authed():
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            handler = ROUTES.get((method, path))
            if handler is None:
                known = any(p == path for (_, p) in ROUTES)
                return self.error(405 if known else 404, "Method not allowed" if known else "Not found")
            if (method, path) not in BODY_ROUTES:     # keep HTTP/1.1 connections in sync
                self.drain_body()
            handler(self, q)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:     # never leak internals to the client
            print(f"[panel] internal error on {self.command} {self.path.split('?')[0]}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            try:
                self.error(500, "Internal server error")
            except Exception:
                pass

    def static(self, path):
        entry = STATIC.get(path)
        if not entry:
            return self.error(404, "Not found")
        fname, ctype = entry
        try:
            with open(os.path.join(WEB_DIR, fname), "rb") as f:
                body = f.read()
        except OSError:
            return self.error(404, "Not found")
        extra = {"Cache-Control": "no-cache"}
        if fname == "index.html":
            extra["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self._headers(200, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)


# ---------------------------------------------------------------------- endpoint handlers
def api_status(h, q):
    s = SUP.status()
    s["server"] = {"ok": True, "version": VERSION, "uptime_seconds": int(time.time() - STARTED),
                   "time": time.time()}
    cfg = STORE.raw()
    s["config_summary"] = {
        "mode": cfg.get("mode"),
        "source": "custom list" if cfg.get("use_customlist") else f"random {cfg.get('letter_count')}-char",
    }
    h.json(s)


def api_get_config(h, q):
    h.json({"config": STORE.public(), "running": SUP.is_running()})


def api_put_config(h, q):
    body = h.body_json()
    if body is None:
        return
    changed, errors = STORE.update(body)
    if errors:
        return h.json({"error": "Invalid configuration", "details": errors}, 400)
    SUP.log("info", "config", "Settings updated" + (f" ({', '.join(c for c in changed if c not in ('webhook_url', 'scrapingant_key'))})"
                                                  if changed else ""))
    h.json({"ok": True, "changed": changed, "restart_required": SUP.is_running(), "config": STORE.public()})


def api_logs(h, q):
    def i(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(q.get(key, default))))
        except ValueError:
            return default
    h.json(SUP.get_logs(since=i("since", 0, 0, 10**12), limit=i("limit", 300, 1, 1000), wait=i("wait", 0, 0, 25)))


def api_logs_clear(h, q):
    SUP.clear_logs()
    h.json({"ok": True})


def _control(fn):
    def handler(h, q):
        ok, msg = fn()
        h.json({"ok": ok, "message": msg, "status": SUP.status()["sniper"]}, 200 if ok else 409)
    return handler


def api_test(h, q):
    result, code = TESTER.run()
    if code == 200:
        SUP.log("success" if result["ok"] else "warning", "test", "Connection test: " + result["message"])
        if not SUP.is_running():
            SUP.set_guns_status("connected" if result["ok"] else "error", result["message"])
    h.json(result, code)


def api_get_customlist(h, q):
    names = STORE.get_customlist()
    h.json({"names": names, "count": len([n for n in names if not n.startswith("//")])})


def api_put_customlist(h, q):
    body = h.body_json()
    if not isinstance(body, dict):
        return h.error(400, "Body must be an object with a 'names' list") if body is not None else None
    ok, errors = STORE.set_customlist(body.get("names"))
    if not ok:
        return h.json({"error": "Invalid list", "details": errors}, 400)
    SUP.log("info", "config", "Custom list updated")
    h.json({"ok": True, "restart_required": SUP.is_running()})


def api_results(h, q):
    h.json({"names": STORE.get_results()})


ROUTES = {
    ("GET", "/api/status"): api_status,
    ("GET", "/api/config"): api_get_config,
    ("PUT", "/api/config"): api_put_config,
    ("GET", "/api/logs"): api_logs,
    ("POST", "/api/logs/clear"): api_logs_clear,
    ("POST", "/api/sniper/start"): _control(SUP.start),
    ("POST", "/api/sniper/stop"): _control(SUP.stop),
    ("POST", "/api/sniper/restart"): _control(SUP.restart),
    ("POST", "/api/test-connection"): api_test,
    ("GET", "/api/customlist"): api_get_customlist,
    ("PUT", "/api/customlist"): api_put_customlist,
    ("GET", "/api/results"): api_results,
}


BODY_ROUTES = {("PUT", "/api/config"), ("PUT", "/api/customlist")}


# ---------------------------------------------------------------------- main
def main():
    global KEY_HASH, FIRST_RUN_KEY
    if "--new-key" in sys.argv:
        key = new_key_file()
        print("\nNew API key (shown once - copy it now, only a hash is stored):\n\n    " + key + "\n")
        return
    KEY_HASH, FIRST_RUN_KEY = load_key_hash()
    os.makedirs(DATA_DIR, exist_ok=True)

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"

    print(f"Guns.lol sniper panel v{VERSION} listening on {scheme}://{HOST}:{PORT}")
    if FIRST_RUN_KEY:
        print("\n  FIRST RUN - your API key (shown once, only a hash is stored):\n\n      " + FIRST_RUN_KEY +
              "\n\n  Lost it?  python server.py --new-key\n")
    if scheme == "http" and HOST not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: listening on a public interface without TLS - your API key would travel in clear text.\n"
              "         Put it behind an HTTPS reverse proxy (see deploy/Caddyfile) or set TLS_CERT / TLS_KEY.")

    if AUTO_RESUME and SUP.desired == "running":
        SUP.start(resumed=True)

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        srv.serve_forever()
    finally:
        SUP.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
