"""Privileged half of the web UI: answers one request per connection, as root.

child-minder-broker.socket listens on BROKER_SOCKET, which only root and the
child-minder-web group may connect to, and starts one broker process per
connection (Accept=yes) with the connection as stdin and stdout. The web
front end, which runs unprivileged, sends one JSON object and shuts down its
side for writing; the broker answers with one JSON object and exits.

Requests are {"op": name, ...} with an op from Broker.OPERATIONS.
Responses are {"result": ...} or {"status": <HTTP status>, "error": message}.

Reads come from the files the daemon and cmctl keep, through the same
modules they use. The usage counters are the ones the daemon saves to
state.json once a minute, so they lag the running daemon by up to a minute.

Every change runs a cmctl subcommand from COMMANDS, so the web UI edits
the files exactly as cmctl does, locks and service reload included.
"""

import base64
import fcntl
import json
import logging
import os
import pwd
import re
import socket
import struct
import subprocess
import sys
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Optional, Tuple

import psutil

from childminder import config as cfg
from childminder import policy
from childminder import schedule
from childminder import store
from childminder import webauth

BROKER_SOCKET = "/run/child-minder-broker.sock"
COMMAND_LOCK_PATH = "/run/child-minder-broker.lock"
CONFIG_PATH = "/etc/child-minder/config.json"
STATE_PATH = "/var/lib/child-minder/state.json"
LOG_PATH = "/var/log/child-minder/minder.log"
USER_CONTROL_PATH = "/var/lib/child-minder/user_control.json"

SERVICE = "child-minder"
CMCTL = Path(__file__).resolve().parent.parent / "cmctl.py"
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10
MAX_LOG_LINES = 1000
LOG_TAIL_BYTES = 256 * 1024
MAX_TEXT_LENGTH = 200
MAX_BATCH = 10
COMMAND_TIMEOUT = 60

# cmctl subcommands the web UI may run: the kinds of their positional
# arguments, and of the --name=value options they accept
COMMANDS = {
    "block": ("text",),
    "unblock": ("text",),
    "limit": ("text", "int"),
    "unlimit": ("text",),
    "add-to-group": ("text", "text"),
    "remove-from-group": ("text", "text"),
    "group-limit": ("text", "int"),
    "group-unlimit": ("text",),
    "user-limit": ("text", "int"),
    "user-weekday-limit": ("text", "int"),
    "user-weekend-limit": ("text", "int"),
    "user-unlimit": ("text",),
    "user-weekday-unlimit": ("text",),
    "user-weekend-unlimit": ("text",),
    "add-user": ("text",),
    "remove-user": ("text",),
    "enable": (),
    "disable": (),
    "reset": (),
    "disable-user": ("text",),
    "enable-user": ("text",),
    "set-weekday-hours": ("text", "int", "int"),
    "set-weekend-hours": ("text", "int", "int"),
    "add-weekday-window": ("text", "int", "int"),
    "add-weekend-window": ("text", "int", "int"),
    "remove-weekday-window": ("text", "int", "int"),
    "remove-weekend-window": ("text", "int", "int"),
    "clear-weekday-windows": ("text",),
    "clear-weekend-windows": ("text",),
    "deny-weekday-access": ("text",),
    "deny-weekend-access": ("text",),
    "clear-user-hours": ("text",),
}
COMMAND_OPTIONS = {
    "disable-user": {"hours": "number", "reason": "text"},
}
SCREENSHOT_TIMEOUT = 20
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
# Local displays only: the capture connects to the display's Unix socket
DISPLAY_RE = re.compile(r'^(unix)?:[0-9]+(\.[0-9]+)?$')
# Run as a script under -I (see its docstring), as the display's owner
X11_CAPTURE = Path(__file__).resolve().parent / "x11.py"

# Commands whose first argument must be a monitored user, so the web
# password cannot lock out an account the daemon does not watch. Unlocking
# is not limited: it only undoes a lock Child Minder applied.
MONITORED_USER_COMMANDS = {"disable-user"}

logger = logging.getLogger("childminder.broker")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def check_value(kind: str, value, what: str) -> str:
    """Return value as a cmctl argument if it is of kind, else raise RequestError"""
    if kind == "text":
        valid = (isinstance(value, str) and 0 < len(value) <= MAX_TEXT_LENGTH
                 and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    elif kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    else:
        valid = cfg.is_number(value)
    if not valid:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"Invalid {what}")
    return str(value)


def command_argv(request) -> list:
    """Turn {"command", "args", "options"} into a cmctl argv, or raise RequestError.

    Positional arguments follow "--", so a value starting with "-" is never
    read as an option. argparse refuses a "--" with nothing after it.
    """
    if not isinstance(request, dict) or request.get("command") not in COMMANDS:
        raise RequestError(HTTPStatus.BAD_REQUEST, "Unknown command")
    name = request["command"]
    kinds = COMMANDS[name]
    args = request.get("args", [])
    options = request.get("options", {})
    if not isinstance(args, list) or len(args) != len(kinds):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} takes {len(kinds)} arguments")
    if not isinstance(options, dict):
        raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid options")
    allowed = COMMAND_OPTIONS.get(name, {})
    argv = [sys.executable, str(CMCTL), name]
    for option, value in options.items():
        if option not in allowed:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} has no option {option}")
        if value is not None:
            argv.append(f"--{option}={check_value(allowed[option], value, option)}")
    if args:
        argv += ["--"] + [check_value(kind, value, f"argument {i + 1} of {name}")
                          for i, (kind, value) in enumerate(zip(kinds, args))]
    return argv


def find_x_session(uid: int, home: str) -> Optional[Tuple[str, str]]:
    """Return (DISPLAY, XAUTHORITY) from one of uid's processes, or None.

    A process without XAUTHORITY in its environment uses ~/.Xauthority.
    """
    for proc in psutil.process_iter(["uids", "environ"]):
        uids = proc.info["uids"]
        if uids is None or uids.real != uid:
            continue
        env = proc.info["environ"] or {}
        display = env.get("DISPLAY", "")
        if not DISPLAY_RE.match(display):
            continue
        xauthority = env.get("XAUTHORITY", "")
        if not xauthority.startswith("/") or any(ord(c) < 32 for c in xauthority):
            xauthority = os.path.join(home, ".Xauthority")
        return display, xauthority
    return None


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


class Broker:
    OPERATIONS = {
        "overview": "overview",
        "config": "config_view",
        "logs": "logs",
        "commands": "run_commands",
        "screenshot": "screenshot",
    }

    def __init__(self, config_path: Path, state_path: Path, log_path: Path,
                 user_control_path: Path, command_lock_path: Path = Path(COMMAND_LOCK_PATH)):
        self.command_lock_path = Path(command_lock_path)
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.user_control = store.UserControlStore(Path(user_control_path))

    def handle(self, request) -> dict:
        """Answer one request; never raises"""
        try:
            if not isinstance(request, dict) or request.get("op") not in self.OPERATIONS:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Unknown operation")
            return {"result": getattr(self, self.OPERATIONS[request["op"]])(request)}
        except RequestError as e:
            return {"status": e.status, "error": str(e)}
        except Exception:
            logger.exception("Error handling a broker request")
            return {"status": HTTPStatus.INTERNAL_SERVER_ERROR, "error": "Internal error"}

    def effective_config(self, warnings: list) -> dict:
        try:
            raw = cfg.read_file(self.config_path)
        except cfg.ConfigError as e:
            raise RequestError(HTTPStatus.SERVICE_UNAVAILABLE, str(e))
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
        if cfg.ALL_USERS in monitored:
            _, uid_max = cfg.read_uid_range()
            names = sorted(p.pw_name for p in pwd.getpwall()
                           if cfg.is_regular_uid(config, p.pw_uid, uid_max))
        else:
            names = list(monitored)
        for extra in (usage.daily, usage.processes, usage.groups,
                      user_control["disabled_users"], user_control["daily_schedules"],
                      config["user_daily_limits"]):
            names += [name for name in extra if name not in names]
        return names

    def overview(self, request: dict) -> dict:
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
        # When the daemon ends sessions outside access hours
        enforcing = config["enabled"] and config["user_control"]["enabled"]
        _, uid_max = cfg.read_uid_range()

        users = []
        for name in self.user_names(config, usage, user_control):
            try:
                uid = pwd.getpwnam(name).pw_uid
            except KeyError:
                uid = None
            monitored = (uid is not None and cfg.is_regular_uid(config, uid, uid_max)
                         and cfg.monitors(config, name))
            sched = schedule.normalize(user_control["daily_schedules"].get(name))
            daily = next((line for line in report["daily"] if line.username == name), None)
            users.append({
                "name": name,
                "disabled": user_control["disabled_users"].get(name),
                "schedule": sched,
                "schedule_days": {day: schedule.format_day(sched, day)
                                  for day in cfg.DAY_TYPES},
                "allowed_now": schedule.is_allowed(sched, now),
                "monitored": monitored,
                "schedule_enforced": enforcing and monitored,
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
            "user_control": config["user_control"]["enabled"],
            "last_reset": state.get("last_reset"),
            "state_saved": saved,
            "users": users,
            "warnings": warnings,
        }

    def config_view(self, request: dict) -> dict:
        warnings = []
        config = self.effective_config(warnings)
        return {"config": config, "warnings": warnings}

    def logs(self, request: dict) -> dict:
        count = request.get("lines", 100)
        if isinstance(count, bool) or not isinstance(count, int):
            raise RequestError(HTTPStatus.BAD_REQUEST, "lines must be a number")
        try:
            return {"lines": tail_lines(self.log_path, max(0, min(count, MAX_LOG_LINES)))}
        except OSError as e:
            raise RequestError(HTTPStatus.SERVICE_UNAVAILABLE, f"Could not read the log: {e}")


    def screenshot(self, request: dict) -> dict:
        """Capture a monitored user's X11 screen as a base64 PNG.

        childminder/x11.py runs as that user, with an environment set here
        rather than theirs: DISPLAY and XAUTHORITY come from the user's own
        processes, so only the user may act on them.
        """
        username = request.get("user")
        check_value("text", username, "user")
        config = self.effective_config([])
        if not cfg.monitors(config, username):
            raise RequestError(HTTPStatus.FORBIDDEN, f"{username} is not a monitored user")
        try:
            pw = pwd.getpwnam(username)
        except KeyError:
            raise RequestError(HTTPStatus.NOT_FOUND, f"User {username} does not exist")
        _, uid_max = cfg.read_uid_range()
        if not cfg.is_regular_uid(config, pw.pw_uid, uid_max):
            raise RequestError(HTTPStatus.FORBIDDEN, f"{username} is a system account")

        session = find_x_session(pw.pw_uid, pw.pw_dir)
        if session is None:
            raise RequestError(HTTPStatus.NOT_FOUND, f"{username} has no X11 session")
        display, xauthority = session
        argv = [sys.executable, "-I", str(X11_CAPTURE), display, xauthority]

        client = request.get("client")
        logger.info(f"Web UI client {client if isinstance(client, str) else 'unknown'} "
                    f"took a screenshot of {username} on {display}")
        env = {"HOME": pw.pw_dir, "USER": username, "LOGNAME": username,
               "PATH": "/usr/bin:/bin"}
        try:
            result = subprocess.run(argv, user=pw.pw_uid, group=pw.pw_gid, extra_groups=[],
                                    env=env, cwd="/", stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=SCREENSHOT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            raise RequestError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Screenshot failed: {e}")
        if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
            detail = result.stderr.decode(errors="replace").strip()[-300:]
            raise RequestError(HTTPStatus.BAD_GATEWAY,
                               f"Screenshot failed: {detail or 'no image produced'}")
        if len(result.stdout) > MAX_SCREENSHOT_BYTES:
            raise RequestError(HTTPStatus.BAD_GATEWAY, "Screenshot is too large")
        return {"user": username, "display": display,
                "taken": datetime.now().isoformat(timespec="seconds"),
                "png": base64.b64encode(result.stdout).decode()}

    def run_commands(self, request: dict) -> dict:
        """Run cmctl commands in order, stopping at the first that fails"""
        commands = request.get("commands")
        if not isinstance(commands, list) or not 0 < len(commands) <= MAX_BATCH:
            raise RequestError(HTTPStatus.BAD_REQUEST,
                               f"Expected a list of 1 to {MAX_BATCH} commands")
        argvs = [command_argv(command) for command in commands]
        client = request.get("client")
        client = client if isinstance(client, str) else "unknown"

        account_users = [c["args"][0] for c in commands
                         if c["command"] in MONITORED_USER_COMMANDS]
        if account_users:
            config = self.effective_config([])
            for user in account_users:
                if not cfg.monitors(config, user):
                    raise RequestError(HTTPStatus.FORBIDDEN,
                                       f"{user} is not a monitored user")

        output = []
        # Broker processes run side by side, and cmctl rewrites config.json
        # without a lock, so commands from different requests take turns
        fd = os.open(self.command_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for argv in argvs:
                logger.info(f"Web UI client {client} ran: cmctl {' '.join(argv[2:])}")
                try:
                    result = subprocess.run(argv, capture_output=True, text=True,
                                            stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired) as e:
                    output.append(f"Error: could not run cmctl: {e}")
                    raise RequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "\n".join(output))
                output.append((result.stdout + result.stderr).strip())
                if result.returncode != 0:
                    raise RequestError(HTTPStatus.UNPROCESSABLE_ENTITY, "\n".join(output))
        finally:
            os.close(fd)
        return {"output": "\n".join(output)}


def peer_allowed(conn: socket.socket) -> bool:
    """Whether the connecting process is root or the web front end's user.

    The socket's mode already limits who can connect; this checks again in
    case the socket file is ever created with a wider mode.
    """
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", creds)
    if uid == 0:
        return True
    try:
        return uid == pwd.getpwnam(webauth.WEB_USER).pw_uid
    except KeyError:
        return False


def read_request(conn: socket.socket):
    """Read one JSON request, up to MAX_REQUEST_BYTES, ending at the peer's shutdown"""
    data = b""
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    if len(data) > MAX_REQUEST_BYTES:
        raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid JSON")


def serve_connection(broker: Broker, conn: socket.socket):
    """Answer the one request on conn"""
    conn.settimeout(REQUEST_TIMEOUT)
    try:
        # Read before refusing: closing a Unix socket with unread data resets
        # the connection, and the peer would see that instead of the answer
        request = read_request(conn)
        if not peer_allowed(conn):
            raise RequestError(HTTPStatus.FORBIDDEN, "Not allowed")
        response = broker.handle(request)
    except RequestError as e:
        response = {"status": e.status, "error": str(e)}
    except OSError as e:
        logger.warning(f"Could not read a broker request: {e}")
        return
    try:
        conn.sendall(json.dumps(response).encode())
    except OSError as e:
        logger.warning(f"Could not send a broker response: {e}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s",
                        stream=sys.stderr)
    broker = Broker(Path(CONFIG_PATH), Path(STATE_PATH), Path(LOG_PATH),
                    Path(USER_CONTROL_PATH))
    # systemd passes the accepted connection as stdin (Accept=yes)
    conn = socket.socket(fileno=0)
    try:
        serve_connection(broker, conn)
    finally:
        conn.close()
