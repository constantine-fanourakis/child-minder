"""
Child Minder - Process Monitor and Control System
A parental control system for monitoring and limiting application usage on Linux
"""

import os
import time
import signal
import logging
import logging.handlers
import argparse
import psutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pwd
import subprocess
import re
import socket

from childminder import accounts
from childminder import config as cfg
from childminder import policy
from childminder import schedule
from childminder import store

# Default paths
DEFAULT_CONFIG_PATH = "/etc/child-minder/config.json"
DEFAULT_STATE_PATH = "/var/lib/child-minder/state.json"
DEFAULT_LOG_PATH = "/var/log/child-minder/minder.log"

# Copy of the last config that parsed, kept beside the state file and used when
# the live config becomes unreadable
LAST_GOOD_CONFIG_NAME = "config.last-good.json"

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Seconds between SIGTERM and SIGKILL when closing an application
TERMINATE_GRACE = 5
# Shortest gap between two attempts to end the same user's session
SESSION_END_INTERVAL = 60
# How long a resolved DISPLAY stays usable before it is looked up again
DISPLAY_CACHE_TTL = 300
NOTIFY_TIMEOUT = 5
COMMAND_TIMEOUT = 10

SOUND_FILES = [
    '/usr/share/sounds/freedesktop/stereo/dialog-warning.oga',
    '/usr/share/sounds/ubuntu/stereo/dialog-warning.ogg',
    '/usr/share/sounds/gnome/default/alerts/glass.ogg',
]


def notify_systemd(message: str):
    """Send a state change such as READY=1 to systemd, when run as a notify service.

    The protocol is one datagram on the Unix socket named by NOTIFY_SOCKET,
    where a leading @ names an abstract socket.
    """
    path = os.environ.get('NOTIFY_SOCKET')
    if not path:
        return
    if path.startswith('@'):
        path = '\0' + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(path)
        sock.sendall(message.encode())


class ChildMinder:
    def __init__(self, config_path: str, state_path: str, log_path: str):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        # Ensure directories exist
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.last_good_config_path = self.state_path.parent / LAST_GOOD_CONFIG_NAME
        self.uid_min, self.uid_max = cfg.read_uid_range()

        self.setup_logging()
        self.config = self.load_config()

        self.state = self.load_state()
        self.usage = policy.Usage.from_state(self.state, self.logger.warning)

        self.session_ended_at: Dict[str, float] = {}
        self.display_cache: Dict[str, Tuple[str, float]] = {}
        self.last_user_control_check: Optional[float] = None

        # Account disables and access schedules, kept beside the state file
        self.user_control = store.UserControlStore(self.state_path.parent / store.USER_CONTROL_NAME)
        # The fallback load_user_control_state keeps if the file is unreadable
        self.user_control_state = store.normalize_user_control({})
        self.user_control_state = self.load_user_control_state()

        self.running = True
        self.stop_signal: Optional[int] = None
        self.reload_requested = False
        self.reset_requested = False

        signal.signal(signal.SIGTERM, self.stop_handler)
        signal.signal(signal.SIGINT, self.stop_handler)
        signal.signal(signal.SIGHUP, self.reload_handler)
        signal.signal(signal.SIGUSR1, self.reset_handler)

    def setup_logging(self):
        """Setup logging configuration.

        Rotates in process because the summary log grows continuously and the
        package ships no logrotate dependency. Output goes to the file only;
        systemd captures stderr into the journal, which would otherwise hold a
        second copy of every line.
        """
        handler = logging.handlers.RotatingFileHandler(
            self.log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[handler]
        )
        self.logger = logging.getLogger("childminder")

    # Signal handlers only set flags; the work happens in the main loop so that
    # it cannot interleave with a state write already in progress.
    def stop_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.stop_signal = signum
        self.running = False

    def reload_handler(self, signum, frame):
        """Handle SIGHUP for config reload"""
        self.reload_requested = True

    def reset_handler(self, signum, frame):
        """Handle SIGUSR1 for an out-of-band usage reset"""
        self.reset_requested = True

    def read_config_file(self, path: Path) -> dict:
        """Read and validate one config file, raising ConfigError if unusable"""
        return cfg.load(path, self.logger.warning, self.uid_min)

    def load_config(self) -> dict:
        """Load configuration, falling back to the last copy that parsed.

        Enforcement must not stop because of a typo in a hand-edited file: an
        unreadable config with no usable fallback would otherwise leave the
        service in a restart loop with every limit silently unenforced.
        """
        try:
            config = self.read_config_file(self.config_path)
        except cfg.ConfigError as e:
            self.logger.error(str(e))
            try:
                config = self.read_config_file(self.last_good_config_path)
            except cfg.ConfigError:
                self.logger.critical(
                    "No usable configuration available, monitoring cannot start")
                raise SystemExit(1)
            self.logger.warning(
                f"Falling back to last known good configuration from {self.last_good_config_path}")
            return config

        self.logger.info("Configuration loaded successfully")
        self.save_last_good_config(config)
        return config

    def save_last_good_config(self, config: dict):
        """Keep a copy of a configuration that parsed cleanly"""
        try:
            store.atomic_write_json(self.last_good_config_path, config)
        except OSError as e:
            self.logger.warning(f"Could not save last known good config: {e}")

    def reload_config(self):
        """Reload configuration, keeping the running config if the new one is bad"""
        self.logger.info("Reloading configuration...")
        try:
            config = self.read_config_file(self.config_path)
        except cfg.ConfigError as e:
            self.logger.error(f"{e}; keeping the configuration already in use")
            return
        self.config = config
        self.save_last_good_config(config)
        self.logger.info("Configuration reloaded successfully")

    def load_state(self) -> dict:
        """Load saved usage, starting afresh if there is none or it is unreadable"""
        try:
            state = store.read_json(self.state_path)
        except store.StoreError as e:
            self.logger.warning(f"Could not load state: {e}")
            state = None
        state = state or {}
        if store.parse_time(state.get("last_reset")) is None:
            if "last_reset" in state:
                self.logger.warning("Ignoring unreadable last_reset in the state file")
            state["last_reset"] = datetime.now().isoformat()
        return state

    def save_state(self):
        """Save current state to file"""
        try:
            store.atomic_write_json(self.state_path, {
                **self.usage.to_state(),
                "last_reset": self.state.get("last_reset", datetime.now().isoformat())
            })
        except Exception as e:
            self.logger.error(f"Error saving state: {e}")

    def clear_usage(self, reason: str):
        """Clear all usage counters and the warnings issued against them"""
        self.logger.info(f"Resetting usage counters: {reason}")
        self.usage = policy.Usage()
        self.session_ended_at = {}
        self.state["last_reset"] = datetime.now().isoformat()
        self.save_state()

    def reset_daily_usage(self):
        """Reset daily usage counters when the date changes"""
        last_reset = store.parse_time(self.state.get("last_reset")) or datetime.now()
        if datetime.now().date() != last_reset.date():
            self.clear_usage("new day")

    def get_username(self, uid: int) -> Optional[str]:
        """Get username from UID"""
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None
            
    def is_regular_account(self, uid: int) -> bool:
        """Check that uid is a regular user, between min_uid and UID_MAX.

        Config validation keeps min_uid at or above UID_MIN.

        Nothing the daemon does as root may target any other account: an
        empty monitored_users list means "every human user", not "init and
        the kernel threads too", and a disabled root would have every root
        process killed. Accounts above UID_MAX are system accounts as well.
        """
        return self.config["min_uid"] <= uid <= self.uid_max

    def should_monitor_user(self, username: str, uid: int) -> bool:
        """Check if user should be monitored; system accounts never are"""
        if not self.is_regular_account(uid):
            return False
        monitored_users = self.config.get("monitored_users", [])
        if monitored_users is None:  # Invalid config value: monitor nobody
            return False
        if not monitored_users:  # If empty, monitor all non-system users
            return True
        return username in monitored_users

    @staticmethod
    def is_protected_process(pid: int, ppid: Optional[int]) -> bool:
        """Check whether a process must never be terminated.

        Guards the daemon against killing itself or its own helpers, which would
        otherwise be possible when the daemon shares a name with a pattern.
        """
        own = os.getpid()
        return pid == 1 or pid == own or ppid == own

    def deliver_warning(self, warning: policy.LimitWarning):
        """Word a limit warning for the user and deliver it.

        A process warning names the application seen under the limit, because
        the key is a config pattern such as "minecraft*" that means nothing to
        the user.
        """
        minutes = warning.minutes
        if warning.remaining_seconds < 60:
            span = "less than 1 minute"
        else:
            span = f"{minutes} minute{'s' if minutes != 1 else ''}"
        if warning.kind == "process":
            title = "Time Limit Warning" if minutes > 1 else "FINAL WARNING"
            message = f"{warning.subject} has {span} remaining today."
            subject = f"{warning.key} time limit"
        elif warning.kind == "group":
            title = "Group Time Limit Warning" if minutes > 1 else "FINAL GROUP WARNING"
            message = (f"Group '{warning.subject}' has {span} remaining today. "
                       f"All apps in this group will close when time expires.")
            subject = f"group '{warning.subject}' time limit"
        else:
            title = "Daily Screen Time Warning" if minutes > 1 else "FINAL SCREEN TIME WARNING"
            message = (f"You have {span} of screen time remaining today. "
                       f"All apps will close when time expires.")
            subject = "daily screen time limit"
        self.emit_warning(warning.username, title, message, minutes, span, subject)

    def emit_warning(self, username: str, title: str, message: str,
                     minutes: int, span: str, subject: str):
        """Deliver a time-limit warning over every channel appropriate to its urgency"""
        try:
            if minutes <= 2:
                message += " SAVE YOUR WORK NOW!"
            urgency = 'critical' if minutes <= 5 else 'normal'

            delivered = self.send_notification(username, title, message, urgency,
                                               play_sound=minutes <= 5)
            if minutes <= 5:
                delivered |= self.write_to_user_terminals(username, f"*** {title} ***\n{message}")
            if minutes <= 2 or not delivered:
                self.broadcast(f"{title}: {message}")

            self.logger.info(f"Warned {username} about {subject} ({span} remaining)")
        except Exception as e:
            self.logger.error(f"Error warning {username} about {subject}: {e}")

    def broadcast(self, message: str):
        """Send a message to all logged-in terminals"""
        try:
            subprocess.run(['wall', message], capture_output=True, timeout=5)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            self.logger.debug(f"Could not broadcast message: {e}")

    def write_to_user_terminals(self, username: str, message: str) -> bool:
        """Write a message directly to any terminals the user has open"""
        terminals = set()
        try:
            for proc in psutil.process_iter(['username', 'terminal']):
                if proc.info['username'] == username and proc.info['terminal']:
                    terminals.add(proc.info['terminal'])
        except Exception as e:
            self.logger.debug(f"Could not list terminals for {username}: {e}")

        written = False
        for terminal in terminals:
            try:
                # Non-blocking: a user who leaves a terminal's output queue
                # full must not be able to stall the root daemon mid-pass.
                # O_NOCTTY: the daemon leads a session without a terminal, so
                # opening a terminal that no session owns would otherwise make
                # it the daemon's, and the user could signal the daemon with
                # Ctrl+C.
                fd = os.open(terminal, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
                try:
                    data = f"\n{message}\n\n".encode()
                    while data:
                        data = data[os.write(fd, data):]
                finally:
                    os.close(fd)
                written = True
            except OSError as e:
                self.logger.debug(f"Could not write to {terminal}: {e}")
        return written

    def terminate_processes(self, targets: list):
        """Terminate a batch of processes, escalating to SIGKILL after a grace period.

        Signalling the whole batch before waiting keeps the grace period to one
        interval no matter how many processes are involved; an application with
        dozens of processes would otherwise stall the monitoring pass for as
        many seconds.
        """
        if not targets:
            return

        signalled = []
        for proc, reason in targets:
            try:
                info = f"{proc.name()} (PID: {proc.pid}, User: {proc.username()})"
                proc.terminate()
                signalled.append((proc, reason, info))
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                self.logger.debug(f"Could not terminate process: {e}")

        gone, alive = psutil.wait_procs([p for p, _, _ in signalled], timeout=TERMINATE_GRACE)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        for _, reason, info in signalled:
            self.logger.warning(f"Terminated process: {info} - Reason: {reason}")

    def get_user_display(self, username: str) -> str:
        """Get the user's X display, caching the result for DISPLAY_CACHE_TTL seconds.

        Finding it means reading /proc/<pid>/environ for the user's processes,
        which is too expensive to repeat for every notification.
        """
        cached = self.display_cache.get(username)
        if cached and time.monotonic() - cached[1] < DISPLAY_CACHE_TTL:
            return cached[0]

        display = ':0'
        try:
            for proc in psutil.process_iter(['username', 'environ']):
                try:
                    if proc.info['username'] != username:
                        continue
                    env = proc.info['environ'] or {}
                    if 'DISPLAY' in env and self.is_valid_display(env['DISPLAY']):
                        display = env['DISPLAY']
                        break
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
        except Exception as e:
            self.logger.debug(f"Could not determine display for {username}: {e}")

        self.display_cache[username] = (display, time.monotonic())
        return display

    def is_valid_display(self, display: str) -> bool:
        """Validate display string to prevent injection"""
        # Valid display format: :N or :N.M or hostname:N
        if not display:
            return False
        return bool(re.match(r'^[a-zA-Z0-9._-]*:[0-9]+(\.[0-9]+)?$', display))

    def run_as_user(self, username: str, argv: list, wait: bool = True):
        """Run a command as another user with a session environment.

        Drops privileges directly rather than shelling out through sudo, whose
        env_reset would discard DBUS_SESSION_BUS_ADDRESS and leave notification
        helpers talking to the wrong bus.
        """
        try:
            pw = pwd.getpwnam(username)
        except KeyError:
            self.logger.warning(f"User {username} not found in passwd")
            return None

        env = {
            'DISPLAY': self.get_user_display(username),
            'DBUS_SESSION_BUS_ADDRESS': f"unix:path=/run/user/{pw.pw_uid}/bus",
            'XDG_RUNTIME_DIR': f"/run/user/{pw.pw_uid}",
            'HOME': pw.pw_dir,
            'USER': username,
            'LOGNAME': username,
            'PATH': '/usr/local/bin:/usr/bin:/bin',
        }
        kwargs = dict(user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait:
                return subprocess.Popen(argv, **kwargs)
            return subprocess.run(argv, timeout=NOTIFY_TIMEOUT, **kwargs)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError) as e:
            self.logger.debug(f"Could not run {argv[0]} as {username}: {e}")
            return None

    def send_notification(self, username: str, title: str, message: str,
                         urgency: str = 'normal', play_sound: bool = False) -> bool:
        """Send a desktop notification to a user, returning whether it was delivered.

        The name is only looked up with pwd and passed to subprocess as part
        of an argv list, so any name the system accepts is safe here.
        """
        if urgency not in ('low', 'normal', 'critical'):
            urgency = 'normal'

        result = self.run_as_user(
            username, ['notify-send', '-u', urgency, '-t', '10000', title, message])
        delivered = result is not None and result.returncode == 0

        if play_sound:
            for sound in SOUND_FILES:
                if os.path.exists(sound):
                    self.run_as_user(username, ['paplay', sound], wait=False)
                    break

        return delivered

    def load_user_control_state(self) -> dict:
        """Load account disables and schedules, keeping the last good copy if unreadable"""
        try:
            return self.user_control.load(self.logger.warning)
        except store.StoreError as e:
            self.logger.error(f"{e}; keeping the account disables and schedules last loaded")
            return self.user_control_state

    def end_user_session(self, username: str, reason: str):
        """End a user's desktop session, at most once per SESSION_END_INTERVAL.

        Throttled because the conditions that call it (a disabled account, time
        outside the allowed window, an exhausted daily limit) stay true until
        the next daily reset. Signalling every process on every pass would stop
        the user from staying logged in long enough to read why. A user with no
        processes has nothing to end, so nothing is logged or attempted.
        """
        now = time.monotonic()
        last = self.session_ended_at.get(username, 0)
        if last and now - last < SESSION_END_INTERVAL:
            return

        try:
            uid = pwd.getpwnam(username).pw_uid
        except KeyError:
            return
        targets = []
        for proc in psutil.process_iter(['pid', 'uids', 'ppid']):
            uids = proc.info['uids']
            if uids is None or uids.real != uid:
                continue
            if self.is_protected_process(proc.info['pid'], proc.info['ppid']):
                continue
            targets.append((proc, reason))
        if not targets:
            return
        self.session_ended_at[username] = now

        self.logger.warning(f"Ending session for {username}: {reason}")
        self.send_notification(username, "Session Ending",
                               f"Your session is being closed: {reason}.", 'critical', True)
        self.write_to_user_terminals(username, f"*** Session ending: {reason} ***")

        # loginctl unwinds the session cleanly where logind manages it; the
        # sweep below covers anything left behind, including non-session
        # processes such as a detached screen or a lingering user service.
        self.run_command(['loginctl', 'terminate-user', username], check=False)

        self.terminate_processes(targets)

    def handle_disabled_user(self, username: str, end_session: bool = True) -> bool:
        """Re-enable a disabled account whose time is up, or end its session.

        Returns True if the account was re-enabled.
        """
        try:
            user_info = self.user_control_state["disabled_users"].get(username)
            if user_info is None:
                return False

            if store.disable_expired(user_info, datetime.now()):
                self.logger.info(f"Timed disable expired for user {username}, re-enabling account")
                if self.enable_user_account(username):
                    return True
            if not end_session:
                return False

            try:
                uid = pwd.getpwnam(username).pw_uid
            except KeyError:
                return False
            if not self.is_regular_account(uid):
                return False

            self.end_user_session(username, f"account disabled ({user_info.get('reason', 'no reason given')})")
            return False

        except Exception as e:
            self.logger.error(f"Error handling disabled user {username}: {e}")
            return False

    def run_command(self, argv: list, check: bool = True):
        """Run a helper command as root, returning the completed process or None.

        The daemon already runs as root, so helpers are invoked directly; going
        through sudo adds a PATH and sudoers failure surface and strips the
        environment that notification helpers depend on.
        """
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            self.logger.error(f"Command {argv[0]} failed: {e}")
            return None
        if check and result.returncode != 0:
            self.logger.error(
                f"Command {' '.join(argv)} failed ({result.returncode}): {result.stderr.strip()}")
        return result

    def run_outside_sandbox(self, argv: list):
        """Run a command as a transient systemd unit, outside this service's sandbox"""
        return self.run_command(accounts.SYSTEMD_RUN + argv, check=False)

    def enable_user_account(self, username: str) -> bool:
        """Re-enable an account whose timed disable has expired, returning whether it was.

        Runs under the user control lock, re-reading the record first: cmctl
        takes the same lock to disable an account, so a disable made after
        the check began is either seen here and kept, or made after this
        unlock has finished.
        """
        try:
            with self.user_control.update(self.logger.warning) as data:
                record = data["disabled_users"].get(username)
                if record is None or not store.disable_expired(record, datetime.now()):
                    return False
                error, note = accounts.unlock(username, self.run_outside_sandbox)
                if error:
                    self.logger.error(f"Failed to re-enable {username}, still disabled: {error}")
                    return False
                if note:
                    self.logger.info(f"Password for {username} stayed locked: {note}")
                del data["disabled_users"][username]
        except (store.StoreError, OSError) as e:
            self.logger.error(f"Error re-enabling {username}: {e}")
            return False

        self.user_control_state = data
        self.session_ended_at.pop(username, None)
        self.logger.info(f"User account re-enabled: {username}")
        return True

    def check_user_access_hours(self, username: str) -> bool:
        """Check whether a user's access schedule allows access now"""
        sched = self.user_control_state["daily_schedules"].get(username)
        return sched is None or schedule.is_allowed(sched, datetime.now())

    def enforce_user_control(self):
        """Apply account disables and access schedules, once per user_control.check_interval.

        Throttled so that a user outside their allowed hours has their session
        ended periodically rather than having every process signalled on every
        monitoring pass. Expired timed disables are lifted whatever the
        settings, since lifting a restriction must not depend on enforcement
        being on; sessions are ended only while monitoring and user control
        are both enabled.
        """
        settings = self.config["user_control"]
        now = time.monotonic()
        last = self.last_user_control_check
        if last is not None and now - last < settings["check_interval"]:
            return
        self.last_user_control_check = now

        self.user_control_state = self.load_user_control_state()
        enforcing = self.config["enabled"] and settings["enabled"]

        # Snapshot keys: handle_disabled_user can re-enable and remove entries
        for username in list(self.user_control_state["disabled_users"]):
            self.handle_disabled_user(username, end_session=enforcing)
        if not enforcing:
            return

        for username in self.user_control_state["daily_schedules"]:
            if username in self.user_control_state["disabled_users"]:
                continue  # already handled above
            try:
                uid = pwd.getpwnam(username).pw_uid
            except KeyError:
                continue
            if not self.should_monitor_user(username, uid):
                continue
            if not self.check_user_access_hours(username):
                self.end_user_session(username, "outside allowed access hours")

    def scan_processes(self) -> List[policy.Process]:
        """List the running processes of monitored users"""
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'uids', 'ppid', 'status']):
            info = proc.info
            uids, name = info['uids'], info['name']
            if uids is None or not name:
                continue
            if info.get('status') == psutil.STATUS_ZOMBIE:
                continue  # Already dead; only its parent can reap it
            
            username = self.get_username(uids.real)
            if not username or not self.should_monitor_user(username, uids.real):
                continue
            processes.append(policy.Process(
                proc, name, username, self.is_protected_process(info['pid'], info['ppid'])))
        return processes

    def monitor_processes(self, elapsed: float):
        """Run one monitoring pass, charging elapsed seconds against active limits"""
        actions = policy.evaluate(self.scan_processes(), self.config, self.usage,
                                  elapsed, datetime.now())
        for warning in actions.warnings:
            self.deliver_warning(warning)
        self.terminate_processes(actions.terminate)
        for username, reason in actions.end_sessions:
            self.end_user_session(username, reason)

    def log_usage_summary(self):
        """Log usage summary periodically"""
        if self.usage.is_empty():
            return

        self.logger.info("=== Usage Summary ===")
        report = policy.usage_report(self.usage, self.config, datetime.now())

        def describe(line: policy.UsageLine) -> str:
            text = f"Time: {line.seconds / 60:.1f} minutes"
            if line.remaining_seconds is not None:
                text += f" (Remaining: {line.remaining_seconds / 60:.0f} minutes)"
            return text

        headings = {"process": ("Individual Process Usage:", "Process: "),
                    "group": ("Group Usage:", "Group: "),
                    "daily": ("User Daily Screen Time:", "")}
        for kind, (heading, label) in headings.items():
            if not report[kind]:
                continue
            self.logger.info(heading)
            for line in report[kind]:
                subject = f"{label}{line.subject}, " if label else ""
                self.logger.info(f"  User: {line.username}, {subject}{describe(line)}")

        self.logger.info("===================")

    def wait_interval(self, seconds: float):
        """Sleep for seconds, waking promptly when a signal handler fires.

        Signal handlers only set flags, and Python retries a plain
        time.sleep() after them, so one long sleep would delay shutdown,
        reload and reset by the whole check_interval.
        """
        deadline = time.monotonic() + seconds
        while self.running and not self.reload_requested and not self.reset_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1))

    def run(self):
        """Main monitoring loop"""
        self.logger.info("Child Minder started")
        # Until READY=1, systemd holds reload jobs and reports the unit as
        # activating, so no SIGHUP or SIGUSR1 can arrive before the handlers
        # installed in __init__, while the signal would still kill the process.
        try:
            notify_systemd("READY=1")
        except OSError as e:
            self.logger.error(f"Could not notify systemd that the service is ready: {e}")

        last_summary = time.monotonic()
        last_save = time.monotonic()
        last_tick = time.monotonic()

        while self.running:
            try:
                if self.reload_requested:
                    self.reload_requested = False
                    self.reload_config()
                if self.reset_requested:
                    self.reset_requested = False
                    self.clear_usage("reset requested")

                self.reset_daily_usage()
                self.enforce_user_control()

                now = time.monotonic()
                if not self.config["enabled"]:
                    last_tick = now
                    self.wait_interval(self.config['check_interval'])
                    continue

                # Charge actual elapsed time rather than the configured interval,
                # which ignores how long each pass takes. The cap keeps a stall
                # or a resume from suspend from being counted as screen time.
                elapsed = min(now - last_tick, self.config['check_interval'] * 2)
                last_tick = now

                self.monitor_processes(elapsed)

                if now - last_summary >= self.config['usage_log_interval']:
                    self.log_usage_summary()
                    last_summary = now

                if now - last_save >= 60:
                    self.save_state()
                    last_save = now

                self.wait_interval(self.config['check_interval'])

            except Exception as e:
                self.logger.exception(f"Error in main loop: {e}")
                time.sleep(5)
                last_tick = time.monotonic()

        if self.stop_signal is not None:
            self.logger.info(f"Received signal {self.stop_signal}, shutting down")
        self.save_state()
        self.logger.info("Child Minder stopped")

def main():
    parser = argparse.ArgumentParser(description='Child Minder - Process Monitor and Control System')
    parser.add_argument('--config', default=DEFAULT_CONFIG_PATH, help='Path to config file')
    parser.add_argument('--state', default=DEFAULT_STATE_PATH, help='Path to state file')
    parser.add_argument('--log', default=DEFAULT_LOG_PATH, help='Path to log file')

    args = parser.parse_args()

    minder = ChildMinder(args.config, args.state, args.log)
    minder.run()
