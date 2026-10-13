"""Web UI: a password-protected dashboard served over HTTP.

Reads come from the files the daemon and cmctl keep, through the same
modules they use. The usage counters are the ones the daemon saves to
state.json once a minute, so they lag the running daemon by up to a minute.

Sessions live in memory, so a restart logs everyone out. The password record
in web.json is read on every login and session check: setting a new password
with cmctl web-password ends existing sessions without a restart.
"""

import hmac
import http.server
import json
import logging
import pwd
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus, cookies
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from childminder import config as cfg
from childminder import policy
from childminder import schedule
from childminder import store
from childminder import webauth

WEB_CONFIG_PATH = "/etc/child-minder/web.json"
CONFIG_PATH = "/etc/child-minder/config.json"
STATE_PATH = "/var/lib/child-minder/state.json"
LOG_PATH = "/var/log/child-minder/minder.log"
USER_CONTROL_PATH = "/var/lib/child-minder/user_control.json"

SERVICE = "child-minder"
DEFAULT_PORT = 8765
DEFAULT_BIND = "0.0.0.0"

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

COOKIE_NAME = "cm_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024
MAX_LOG_LINES = 1000
LOG_TAIL_BYTES = 256 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: blob:; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

logger = logging.getLogger("childminder.web")


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def load_settings(path: Path) -> dict:
    """Return the bind address and port from web.json, defaulting each invalid one"""
    try:
        raw = store.read_json(path) or {}
    except store.StoreError as e:
        logger.warning(f"{e}; using default settings")
        raw = {}
    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        logger.warning(f"Invalid port in {path}, using {DEFAULT_PORT}")
        port = DEFAULT_PORT
    bind = raw.get("bind", DEFAULT_BIND)
    if not isinstance(bind, str) or not bind:
        logger.warning(f"Invalid bind in {path}, using {DEFAULT_BIND}")
        bind = DEFAULT_BIND
    return {"bind": bind, "port": port}


def tail_lines(path: Path, count: int) -> list:
    """Return the last count lines of a text file, reading at most LOG_TAIL_BYTES"""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - LOG_TAIL_BYTES))
        data = f.read()
    lines = data.decode(errors="replace").splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # The first line read is most likely partial
    return lines[-count:] if count else []


def usage_line(line: Optional[policy.UsageLine], seconds: float = 0.0,
               limit_seconds: Optional[float] = None) -> dict:
    if line is not None:
        seconds, limit_seconds = line.seconds, line.limit_seconds
    remaining = None if limit_seconds is None else max(0.0, limit_seconds - seconds)
    return {"seconds": seconds, "limit_seconds": limit_seconds,
            "remaining_seconds": remaining}


@dataclass
class Session:
    csrf: str
    expires: float      # time.monotonic() deadline
    salt: str           # salt of the password record it logged in with


class WebApp:
    """State shared by every request: sessions, login failures and file paths"""

    def __init__(self, web_config_path: Path, config_path: Path, state_path: Path,
                 log_path: Path, user_control_path: Path):
        self.web_config_path = Path(web_config_path)
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.user_control = store.UserControlStore(Path(user_control_path))
        self.sessions: Dict[str, Session] = {}
        self.failures: Dict[str, Tuple[int, float]] = {}  # ip: (count, locked until)
        self.lock = threading.Lock()
        # One scrypt at a time, so a flood of logins cannot exhaust memory
        self.verify_lock = threading.Lock()

    # -- authentication --

    def password_record(self) -> Optional[dict]:
        """The stored password hash, or None if none is set or web.json is unusable"""
        try:
            record = (store.read_json(self.web_config_path) or {}).get("password")
        except store.StoreError as e:
            logger.warning(str(e))
            return None
        return record if isinstance(record, dict) else None

    def login(self, ip: str, password: str) -> Tuple[str, Session]:
        now = time.monotonic()
        with self.lock:
            count, locked_until = self.failures.get(ip, (0, 0.0))
            if locked_until > now:
                raise ApiError(HTTPStatus.TOO_MANY_REQUESTS,
                               "Too many failed logins; try again later",
                               {"Retry-After": str(int(locked_until - now) + 1)})

        record = self.password_record()
        if record is None:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE,
                           "No password is set; run: sudo cmctl web-password")
        with self.verify_lock:
            matched = webauth.verify_password(password, record)

        with self.lock:
            if not matched:
                count, locked_until = self.failures.get(ip, (0, 0.0))
                count += 1
                if count >= MAX_FAILURES:
                    self.failures[ip] = (0, time.monotonic() + LOCKOUT_SECONDS)
                    logger.warning(f"Failed login from {ip}; locked out for "
                                   f"{LOCKOUT_SECONDS} seconds")
                else:
                    self.failures[ip] = (count, 0.0)
                    logger.warning(f"Failed login from {ip}")
                raise ApiError(HTTPStatus.UNAUTHORIZED, "Wrong password")
            self.failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            session = Session(secrets.token_urlsafe(32), time.monotonic() + SESSION_SECONDS,
                              str(record.get("salt")))
            self.sessions[token] = session
            self._drop_expired()
        logger.info(f"Login from {ip}")
        return token, session

    def session(self, token: Optional[str]) -> Optional[Session]:
        """The live session for a cookie token, if any"""
        if not token:
            return None
        record = self.password_record()
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            if (session.expires <= time.monotonic() or record is None
                    or not hmac.compare_digest(session.salt, str(record.get("salt")))):
                del self.sessions[token]
                return None
            return session

    def logout(self, token: Optional[str]):
        with self.lock:
            self.sessions.pop(token, None)

    def _drop_expired(self):
        """Forget expired sessions and lockouts; the caller holds self.lock"""
        now = time.monotonic()
        for token in [t for t, s in self.sessions.items() if s.expires <= now]:
            del self.sessions[token]
        for ip in [ip for ip, (count, until) in self.failures.items()
                   if count == 0 and until <= now]:
            del self.failures[ip]

    # -- reads --

    def effective_config(self, warnings: list) -> dict:
        try:
            raw = cfg.read_file(self.config_path)
        except cfg.ConfigError as e:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(e))
        uid_min, _ = cfg.read_uid_range()
        return cfg.validate(raw, warnings.append, uid_min)

    def service_state(self) -> str:
        try:
            result = subprocess.run(["systemctl", "is-active", SERVICE],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return result.stdout.strip() or "unknown"

    def user_names(self, config: dict, usage: policy.Usage, user_control: dict) -> list:
        """Users to show: the monitored ones, plus any with usage, limits or records"""
        monitored = config["monitored_users"]
        if monitored is None:
            names = []
        elif monitored:
            names = list(monitored)
        else:
            _, uid_max = cfg.read_uid_range()
            names = sorted(p.pw_name for p in pwd.getpwall()
                           if config["min_uid"] <= p.pw_uid <= uid_max)
        for extra in (usage.daily, usage.processes, usage.groups,
                      user_control["disabled_users"], user_control["daily_schedules"],
                      config["user_daily_limits"]):
            names += [name for name in extra if name not in names]
        return names

    def overview(self) -> dict:
        now = datetime.now()
        warnings = []
        config = self.effective_config(warnings)
        try:
            state = store.read_json(self.state_path) or {}
        except store.StoreError as e:
            warnings.append(str(e))
            state = {}
        usage = policy.Usage.from_state(state, warnings.append)
        try:
            user_control = self.user_control.load(warnings.append)
        except store.StoreError as e:
            warnings.append(str(e))
            user_control = store.normalize_user_control({})
        report = policy.usage_report(usage, config, now)

        users = []
        for name in self.user_names(config, usage, user_control):
            sched = schedule.normalize(user_control["daily_schedules"].get(name))
            daily = next((line for line in report["daily"] if line.username == name), None)
            users.append({
                "name": name,
                "disabled": user_control["disabled_users"].get(name),
                "schedule": sched,
                "schedule_days": {day: schedule.format_day(sched, day)
                                  for day in cfg.DAY_TYPES},
                "allowed_now": schedule.is_allowed(sched, now),
                "daily": usage_line(daily, limit_seconds=policy.daily_limit(config, name, now)),
                "processes": [dict(usage_line(line), subject=line.subject)
                              for line in report["process"] if line.username == name],
                "groups": [dict(usage_line(line), subject=line.subject)
                           for line in report["group"] if line.username == name],
            })

        try:
            saved = datetime.fromtimestamp(self.state_path.stat().st_mtime).isoformat()
        except OSError:
            saved = None
        return {
            "now": now.isoformat(timespec="seconds"),
            "day_type": cfg.day_type(now),
            "service": self.service_state(),
            "monitoring": config["enabled"],
            "last_reset": state.get("last_reset"),
            "state_saved": saved,
            "users": users,
            "warnings": warnings,
        }

    def config_view(self) -> dict:
        warnings = []
        config = self.effective_config(warnings)
        return {"config": config, "warnings": warnings}

    def logs(self, count: int) -> dict:
        try:
            return {"lines": tail_lines(self.log_path, count)}
        except OSError as e:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"Could not read the log: {e}")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "child-minder"
    sys_version = ""
    timeout = 30    # seconds a client may hold a connection without sending

    # (method, path): (handler method name, needs a session)
    ROUTES = {
        ("GET", "/api/session"): ("get_session", False),
        ("POST", "/api/login"): ("post_login", False),
        ("POST", "/api/logout"): ("post_logout", True),
        ("GET", "/api/overview"): ("get_overview", True),
        ("GET", "/api/config"): ("get_config", True),
        ("GET", "/api/logs"): ("get_logs", True),
    }

    @property
    def app(self) -> WebApp:
        return self.server.app

    def log_message(self, format, *args):
        logger.debug(f"{self.client_address[0]} {format % args}")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method: str):
        url = urlsplit(self.path)
        self.query = parse_qs(url.query)
        self.response_headers = {}
        try:
            if method == "GET" and url.path in STATIC_FILES:
                self.send_static(*STATIC_FILES[url.path])
                return
            route = self.ROUTES.get((method, url.path))
            if route is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
            name, needs_session = route
            self.current = None
            if needs_session:
                self.current = self.app.session(self.cookie_token())
                if self.current is None:
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "Not logged in")
            body = None
            if method == "POST":
                if needs_session and not hmac.compare_digest(
                        self.headers.get("X-CSRF-Token", ""), self.current.csrf):
                    raise ApiError(HTTPStatus.FORBIDDEN, "Missing or invalid CSRF token")
                body = self.read_body()
            self.send_json(HTTPStatus.OK, getattr(self, name)(body))
        except ApiError as e:
            self.response_headers.update(e.headers)
            self.send_json(e.status, {"error": str(e)})
        except Exception:
            logger.exception(f"Error handling {method} {url.path}")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error"})

    def cookie_token(self) -> Optional[str]:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def read_body(self) -> dict:
        """Parse a JSON object body.

        Requiring application/json means a cross-site HTML form, which cannot
        send that content type, cannot reach any POST endpoint.
        """
        if self.headers.get_content_type() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Expected a JSON object")
        return body

    def send_bytes(self, status: int, data: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for header, value in {**SECURITY_HEADERS, **self.response_headers}.items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, payload: dict):
        self.send_bytes(status, json.dumps(payload).encode(), "application/json")

    def send_static(self, name: str, content_type: str):
        try:
            data = (STATIC_DIR / name).read_bytes()
        except OSError:
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
        self.send_bytes(HTTPStatus.OK, data, content_type)

    def set_session_cookie(self, token: str, max_age: int):
        # No Secure attribute: the UI is served over plain HTTP
        self.response_headers["Set-Cookie"] = (
            f"{COOKIE_NAME}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict")

    # -- endpoints --

    def get_session(self, body) -> dict:
        session = self.app.session(self.cookie_token())
        return {"authenticated": session is not None,
                "csrf": session.csrf if session else None,
                "password_set": self.app.password_record() is not None}

    def post_login(self, body: dict) -> dict:
        password = body.get("password")
        if not isinstance(password, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Expected a password")
        token, session = self.app.login(self.client_address[0], password)
        self.set_session_cookie(token, SESSION_SECONDS)
        return {"authenticated": True, "csrf": session.csrf}

    def post_logout(self, body: dict) -> dict:
        self.app.logout(self.cookie_token())
        self.set_session_cookie("", 0)
        return {"authenticated": False}

    def get_overview(self, body) -> dict:
        return self.app.overview()

    def get_config(self, body) -> dict:
        return self.app.config_view()

    def get_logs(self, body) -> dict:
        try:
            count = int(self.query.get("lines", ["100"])[0])
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "lines must be a number")
        return self.app.logs(max(0, min(count, MAX_LOG_LINES)))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple, app: WebApp):
        super().__init__(address, Handler)
        self.app = app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s",
                        stream=sys.stderr)
    settings = load_settings(Path(WEB_CONFIG_PATH))
    app = WebApp(Path(WEB_CONFIG_PATH), Path(CONFIG_PATH), Path(STATE_PATH),
                 Path(LOG_PATH), Path(USER_CONTROL_PATH))
    if app.password_record() is None:
        logger.warning("No password is set, so every login is refused; "
                       "set one with: sudo cmctl web-password")
    server = Server((settings["bind"], settings["port"]), app)
    logger.info(f"Serving the web UI on {settings['bind']}:{settings['port']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
