#!/usr/bin/env python3
"""
Child Minder - Process Monitor and Control System
A parental control system for monitoring and limiting application usage on Linux
"""

import os
import time
import json
import math
import signal
import logging
import logging.handlers
import argparse
import psutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, Optional, Tuple
import pwd
import subprocess
import re
import fnmatch

# Default paths
DEFAULT_CONFIG_PATH = "/etc/child-minder/config.json"
DEFAULT_STATE_PATH = "/var/lib/child-minder/state.json"
DEFAULT_LOG_PATH = "/var/log/child-minder/minder.log"

# Copy of the last config that parsed, kept beside the state file and used when
# the live config becomes unreadable
LAST_GOOD_CONFIG_NAME = "config.last-good.json"
# Account disables and access schedules written by cmctl, kept beside the state file
USER_CONTROL_NAME = "user_control.json"

# UID_MAX bounds regular user accounts; above it sit nobody (65534) and
# systemd DynamicUser accounts (61184-65519)
LOGIN_DEFS_PATH = "/etc/login.defs"
DEFAULT_UID_MAX = 60000

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


class ConfigError(Exception):
    """Raised when a configuration file cannot be read or parsed"""


def read_uid_max(path: str = LOGIN_DEFS_PATH) -> int:
    """Return the highest UID for regular users, from login.defs if it sets one"""
    try:
        with open(path) as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2 and fields[0] == "UID_MAX":
                    return int(fields[1])
    except (OSError, ValueError):
        pass
    return DEFAULT_UID_MAX


class ChildMinder:
    def __init__(self, config_path: str, state_path: str, log_path: str):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        # Ensure directories exist
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.last_good_config_path = self.state_path.parent / LAST_GOOD_CONFIG_NAME
        self.max_uid = read_uid_max()

        self.setup_logging()
        self.config = self.load_config()

        # Load or initialize state
        self.state = self.load_state()
        self.daily_usage: Dict[str, Dict[str, float]] = {}
        self.deserialize_usage(self.state.get("daily_usage", {}))
        self.group_usage: Dict[str, Dict[str, float]] = self.state.get("group_usage", {})
        self.user_daily_usage: Dict[str, float] = self.state.get("user_daily_usage", {})

        # Warning thresholds already used, so each one fires at most once a day
        self.warned_limits: Dict[Tuple[str, str], Set[int]] = {}   # (user, process key)
        self.warned_groups: Dict[Tuple[str, str], Set[int]] = {}   # (user, group)
        self.warned_users: Dict[str, Set[int]] = {}                # user

        self.session_ended_at: Dict[str, float] = {}
        self.display_cache: Dict[str, Tuple[str, float]] = {}
        self.last_user_control_check = 0.0

        # User control state file
        self.user_control_file = self.state_path.parent / USER_CONTROL_NAME
        self.user_control_state = self.load_user_control_state()

        self.running = True
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
        self.logger = logging.getLogger(__name__)

    # Signal handlers only set flags; the work happens in the main loop so that
    # it cannot interleave with a state write already in progress.
    def stop_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def reload_handler(self, signum, frame):
        """Handle SIGHUP for config reload"""
        self.reload_requested = True

    def reset_handler(self, signum, frame):
        """Handle SIGUSR1 for an out-of-band usage reset"""
        self.reset_requested = True

    def read_config_file(self, path: Path) -> dict:
        """Read and validate one config file, raising ConfigError if unusable"""
        try:
            with open(path, 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            raise ConfigError(f"Config file not found at {path}")
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {path}: {e}")
        except OSError as e:
            raise ConfigError(f"Could not read {path}: {e}")
        if not isinstance(config, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        return self.validate_config(config)

    def load_config(self) -> dict:
        """Load configuration, falling back to the last copy that parsed.

        Enforcement must not stop because of a typo in a hand-edited file: an
        unreadable config with no usable fallback would otherwise leave the
        service in a restart loop with every limit silently unenforced.
        """
        try:
            config = self.read_config_file(self.config_path)
        except ConfigError as e:
            self.logger.error(str(e))
            try:
                config = self.read_config_file(self.last_good_config_path)
            except ConfigError:
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
            self.atomic_write_json(self.last_good_config_path, config)
        except OSError as e:
            self.logger.warning(f"Could not save last known good config: {e}")

    def reload_config(self):
        """Reload configuration, keeping the running config if the new one is bad"""
        self.logger.info("Reloading configuration...")
        try:
            config = self.read_config_file(self.config_path)
        except ConfigError as e:
            self.logger.error(f"{e}; keeping the configuration already in use")
            return
        self.config = config
        self.save_last_good_config(config)
        self.logger.info("Configuration reloaded successfully")

    def validate_config(self, config: dict) -> dict:
        """Validate and fill in missing config keys with defaults"""
        defaults = self.get_default_config()

        # Ensure all required keys exist
        for key, default_value in defaults.items():
            if key not in config:
                config[key] = default_value
            elif isinstance(default_value, dict) and isinstance(config[key], dict):
                # Recursively fill in nested dicts
                for subkey, subdefault in default_value.items():
                    if subkey not in config[key]:
                        config[key][subkey] = subdefault

        # Validate specific values
        if not isinstance(config.get('check_interval'), (int, float)) or config['check_interval'] <= 0:
            self.logger.warning(f"Invalid check_interval, using default: 5")
            config['check_interval'] = 5

        if not isinstance(config.get('min_uid'), int) or config['min_uid'] < 0:
            self.logger.warning("Invalid min_uid, using default: 1000")
            config['min_uid'] = 1000

        for key, expected in (('monitored_users', list), ('blocked_processes', list),
                              ('monitored_processes', list), ('limited_processes', dict),
                              ('process_groups', dict), ('group_limits', dict),
                              ('user_daily_limits', dict), ('user_control', dict)):
            if not isinstance(config.get(key), expected):
                self.logger.warning(f"Invalid '{key}', using default")
                config[key] = expected()

        return config

    def get_default_config(self) -> dict:
        """Return default configuration"""
        return {
            "enabled": True,
            "check_interval": 5,
            "min_uid": 1000,
            "monitored_users": [],
            "blocked_processes": [],
            "limited_processes": {},
            "process_groups": {},
            "group_limits": {},
            "user_daily_limits": {},
            "monitored_processes": [],
            "warning_time": 300,
            "usage_log_interval": 60,
            "user_control": {
                "enabled": False,
                "check_interval": 60
            }
        }

    def load_state(self) -> dict:
        """Load saved state from file"""
        try:
            if self.state_path.exists():
                with open(self.state_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.warning(f"Could not load state: {e}")

        return {
            "daily_usage": {},
            "group_usage": {},
            "user_daily_usage": {},
            "last_reset": datetime.now().isoformat()
        }

    @staticmethod
    def atomic_write_json(path: Path, data: dict):
        """Write JSON to path atomically, so a reader never sees a partial file"""
        temp_file = path.with_name(path.name + '.tmp')
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp_file.replace(path)

    def save_state(self):
        """Save current state to file"""
        try:
            self.atomic_write_json(self.state_path, {
                "daily_usage": self.serialize_usage(),
                "group_usage": self.group_usage,
                "user_daily_usage": self.user_daily_usage,
                "last_reset": self.state.get("last_reset", datetime.now().isoformat())
            })
        except Exception as e:
            self.logger.error(f"Error saving state: {e}")

    def serialize_usage(self) -> dict:
        """Serialize daily usage data for saving"""
        serialized = {}
        for user, processes in self.daily_usage.items():
            serialized[user] = {}
            for process, seconds in processes.items():
                serialized[user][process] = seconds
        return serialized

    def deserialize_usage(self, data: dict):
        """Deserialize saved usage data"""
        self.daily_usage = {}
        for user, processes in data.items():
            self.daily_usage[user] = {}
            for process, seconds in processes.items():
                self.daily_usage[user][process] = float(seconds)

    def clear_usage(self, reason: str):
        """Clear all usage counters and the warnings issued against them"""
        self.logger.info(f"Resetting usage counters: {reason}")
        self.daily_usage = {}
        self.group_usage = {}
        self.user_daily_usage = {}
        self.warned_limits = {}
        self.warned_groups = {}
        self.warned_users = {}
        self.session_ended_at = {}
        self.state["last_reset"] = datetime.now().isoformat()
        self.save_state()

    def reset_daily_usage(self):
        """Reset daily usage counters when the date changes"""
        try:
            last_reset = datetime.fromisoformat(
                self.state.get("last_reset", datetime.now().isoformat()))
        except ValueError:
            last_reset = datetime.now()
        if datetime.now().date() != last_reset.date():
            self.clear_usage("new day")

    def get_username(self, uid: int) -> Optional[str]:
        """Get username from UID"""
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None
            
    def should_monitor_user(self, username: str, uid: int) -> bool:
        """Check if user should be monitored.

        System accounts are never monitored regardless of configuration: the
        daemon terminates processes as root, and an empty monitored_users list
        means "every human user", not "init and the kernel threads too".
        Accounts above UID_MAX are system accounts as well.
        """
        if uid < self.config.get("min_uid", 1000) or uid > self.max_uid:
            return False
        monitored_users = self.config.get("monitored_users", [])
        if not monitored_users:  # If empty, monitor all non-system users
            return True
        return username in monitored_users

    def is_protected_process(self, proc: psutil.Process) -> bool:
        """Check whether a process must never be terminated.

        Guards the daemon against killing itself or its own helpers, which would
        otherwise be possible when the daemon shares a name with a pattern.
        """
        return proc.pid == 1 or proc.pid == os.getpid() or proc.ppid() == os.getpid()

    
    @staticmethod
    def pattern_matches(pattern: str, process_name: str) -> bool:
        """Match a config pattern against a process name.

        Patterns are case-insensitive. A pattern containing any of * ? [ is
        treated as a shell glob; anything else must match the whole name.
        Substring matching is deliberately not the default: an entry like "sh"
        would otherwise match bash, zsh and kernel threads, and these patterns
        decide what the daemon terminates as root.
        """
        name = os.path.basename(process_name).lower()
        pattern = pattern.lower()
        if any(c in pattern for c in '*?['):
            return fnmatch.fnmatchcase(name, pattern)
        return name == pattern

    def match_pattern(self, patterns, process_name: str) -> Optional[str]:
        """Return the first pattern from patterns matching process_name, if any"""
        for pattern in patterns:
            if self.pattern_matches(pattern, process_name):
                return pattern
        return None

    def is_process_blocked(self, process_name: str) -> bool:
        """Check if process is in block list"""
        return self.match_pattern(self.config.get("blocked_processes", []), process_name) is not None

    def get_process_group(self, process_name: str) -> Optional[str]:
        """Get the group name for a process, if any.

        A process listed in several groups is attributed to the first group that
        matches in config order.
        """
        for group_name, processes in self.config.get("process_groups", {}).items():
            if self.match_pattern(processes, process_name):
                return group_name
        return None

    
    def get_group_limit(self, group_name: str) -> Optional[int]:
        """Get time limit for a group in seconds"""
        group_limits = self.config.get("group_limits", {})
        if group_name in group_limits:
            return group_limits[group_name] * 60  # Convert to seconds
        return None
    
    def update_group_usage(self, username: str, group_name: str, seconds: float):
        """Update group usage statistics"""
        if username not in self.group_usage:
            self.group_usage[username] = {}
        if group_name not in self.group_usage[username]:
            self.group_usage[username][group_name] = 0
        self.group_usage[username][group_name] += seconds
    
    def warning_intervals(self) -> list:
        """Warning thresholds in seconds, largest first"""
        intervals = self.config.get('warning_intervals')
        if not intervals:
            intervals = [self.config.get('warning_time', 300)]
        return sorted(intervals, reverse=True)

    def consume_warning(self, consumed: Set[int], remaining_seconds: float) -> Optional[int]:
        """Return the largest not-yet-used threshold that remaining_seconds has crossed.

        Every crossed threshold is marked used, not just the one returned. A
        process started with little quota left crosses several thresholds at
        once; marking only one would emit a further warning on each following
        pass until the list was exhausted.
        """
        crossed = [w for w in self.warning_intervals() if remaining_seconds <= w]
        fresh = [w for w in crossed if w not in consumed]
        consumed.update(crossed)
        return max(fresh) if fresh else None

    def remaining_minutes(self, remaining_seconds: float) -> int:
        """Round remaining seconds up to whole minutes for display"""
        return max(0, math.ceil(remaining_seconds / 60))

    def maybe_warn_process(self, username: str, key: str, remaining_seconds: float):
        """Warn about an individual process limit if a threshold has just been crossed"""
        consumed = self.warned_limits.setdefault((username, key), set())
        if self.consume_warning(consumed, remaining_seconds) is None:
            return
        minutes = self.remaining_minutes(remaining_seconds)
        self.emit_warning(
            username,
            "Time Limit Warning" if minutes > 1 else "FINAL WARNING",
            f"{key} has {minutes} minute{'s' if minutes != 1 else ''} remaining today.",
            minutes,
            f"{key} time limit",
        )

    def maybe_warn_group(self, username: str, group_name: str, remaining_seconds: float):
        """Warn about a group limit if a threshold has just been crossed"""
        consumed = self.warned_groups.setdefault((username, group_name), set())
        if self.consume_warning(consumed, remaining_seconds) is None:
            return
        minutes = self.remaining_minutes(remaining_seconds)
        self.emit_warning(
            username,
            "Group Time Limit Warning" if minutes > 1 else "FINAL GROUP WARNING",
            f"Group '{group_name}' has {minutes} minute{'s' if minutes != 1 else ''} remaining "
            f"today. All apps in this group will close when time expires.",
            minutes,
            f"group '{group_name}' time limit",
        )

    def maybe_warn_user_daily(self, username: str, remaining_seconds: float):
        """Warn about the overall daily screen time limit if a threshold has been crossed"""
        consumed = self.warned_users.setdefault(username, set())
        if self.consume_warning(consumed, remaining_seconds) is None:
            return
        minutes = self.remaining_minutes(remaining_seconds)
        self.emit_warning(
            username,
            "Daily Screen Time Warning" if minutes > 1 else "FINAL SCREEN TIME WARNING",
            f"You have {minutes} minute{'s' if minutes != 1 else ''} of screen time remaining "
            f"today. All apps will close when time expires.",
            minutes,
            "daily screen time limit",
        )

    def emit_warning(self, username: str, title: str, message: str,
                     minutes: int, subject: str):
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

            self.logger.info(f"Warned {username} about {subject} ({minutes} min remaining)")
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
        written = False
        try:
            for proc in psutil.process_iter(['username', 'terminal']):
                try:
                    if proc.info['username'] != username or not proc.info['terminal']:
                        continue
                    with open(f"/dev/{proc.info['terminal']}", 'w') as tty:
                        tty.write(f"\n{message}\n\n")
                    written = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, IOError, OSError):
                    continue
        except Exception as e:
            self.logger.debug(f"Could not write to terminals for {username}: {e}")
        return written

    def get_process_limit(self, process_name: str):
        """Return (config key, limit in seconds) for a limited process, or (None, None).

        Usage is accumulated under the config key rather than the observed
        process name so that helper processes with different names (chrome and
        chrome_crashpad_handler, say) share one counter with the limit.
        """
        for limited_name, limit_minutes in self.config.get("limited_processes", {}).items():
            if self.pattern_matches(limited_name, process_name):
                return limited_name, limit_minutes * 60
        return None, None

    def get_monitored_key(self, process_name: str) -> Optional[str]:
        """Return the monitored_processes config key matching this process, if any"""
        return self.match_pattern(self.config.get("monitored_processes", []), process_name)

    
    def update_usage(self, username: str, process_name: str, seconds: float):
        """Update usage statistics"""
        if username not in self.daily_usage:
            self.daily_usage[username] = {}
        if process_name not in self.daily_usage[username]:
            self.daily_usage[username][process_name] = 0
        self.daily_usage[username][process_name] += seconds
        
    def get_usage(self, username: str, process_name: str) -> float:
        """Get current usage in seconds"""
        return self.daily_usage.get(username, {}).get(process_name, 0)
        
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

    def is_valid_username(self, username: str) -> bool:
        """Validate username to prevent injection attacks"""
        # Valid Unix usernames: alphanumeric, underscore, hyphen, start with letter/underscore
        if not username or len(username) > 32:
            return False
        return bool(re.match(r'^[a-z_][a-z0-9_-]*$', username, re.IGNORECASE))

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
        """Send a desktop notification to a user, returning whether it was delivered"""
        if not self.is_valid_username(username):
            self.logger.error(f"Invalid username for notification: {username}")
            return False

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
        """Load user control state from file"""
        try:
            if self.user_control_file.exists():
                with open(self.user_control_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.warning(f"Could not load user control state: {e}")

        return {
            "disabled_users": {},
            "daily_schedules": {}
        }

    def get_group_usage(self, username: str, group_name: str) -> float:
        """Get current group usage in seconds"""
        return self.group_usage.get(username, {}).get(group_name, 0)

    def get_user_daily_limit(self, username: str) -> Optional[int]:
        """Get user's daily screen time limit in seconds for today (weekday vs weekend)"""
        limits = self.config.get("user_daily_limits", {})
        if username not in limits:
            return None
        entry = limits[username]
        if isinstance(entry, (int, float)):
            return int(entry) * 60  # backward compat: plain number
        is_weekend = datetime.now().weekday() >= 5
        key = "weekend" if is_weekend else "weekday"
        val = entry.get(key)
        return val * 60 if val is not None else None

    def get_user_daily_usage(self, username: str) -> float:
        """Get current user daily usage in seconds"""
        return self.user_daily_usage.get(username, 0)

    def update_user_daily_usage(self, username: str, seconds: float):
        """Increment user daily usage"""
        self.user_daily_usage.setdefault(username, 0)
        self.user_daily_usage[username] += seconds

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

        targets = []
        for proc in psutil.process_iter(['pid', 'uids']):
            try:
                uids = proc.info['uids']
                if uids is None or self.get_username(uids.real) != username:
                    continue
                if self.is_protected_process(proc):
                    continue
                targets.append((proc, reason))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
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

    def handle_disabled_user(self, username: str) -> bool:
        """Handle a disabled user account - check for auto-restore or kill processes

        Returns True if user was re-enabled, False otherwise.
        """
        try:
            user_info = self.user_control_state.get("disabled_users", {}).get(username, {})
            if not user_info:
                return False

            # Check if timed disable has expired
            re_enable_at = user_info.get("re_enable_at")
            if re_enable_at:
                re_enable_time = datetime.fromisoformat(re_enable_at)
                if datetime.now() >= re_enable_time:
                    # Time has passed, re-enable the user
                    self.logger.info(f"Timed disable expired for user {username}, re-enabling account")
                    self.enable_user_account(username)
                    return True

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

    def enable_user_account(self, username: str):
        """Re-enable a disabled user account"""
        try:
            # Clearing the expiry is what actually restores login; passwd -u is
            # best effort because it refuses to unlock a passwordless account.
            result = self.run_command(['usermod', '--expiredate', '', username])
            if result is None or result.returncode != 0:
                self.logger.error(f"Failed to clear account expiry for {username}, still disabled")
                return
            self.run_command(['passwd', '-u', username], check=False)

            if username in self.user_control_state.get("disabled_users", {}):
                del self.user_control_state["disabled_users"][username]
                self.save_user_control_state()
            self.session_ended_at.pop(username, None)

            self.logger.info(f"User account re-enabled: {username}")

        except Exception as e:
            self.logger.error(f"Error enabling user account {username}: {e}")

    def save_user_control_state(self):
        """Save the user control state to file"""
        try:
            self.user_control_file.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file then rename
            temp_file = self.user_control_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(self.user_control_state, f, indent=2)
            temp_file.rename(self.user_control_file)
        except Exception as e:
            self.logger.error(f"Error saving user control state: {e}")

    def _check_time_in_window(self, current_hour: int, start_hour: int, end_hour: int) -> bool:
        """Check if current hour falls within a time window.

        Supports both regular hours (e.g., 8-22) and overnight hours (e.g., 22-6).
        """
        if start_hour > end_hour:
            # Overnight schedule: allowed if current hour is >= start OR < end
            return current_hour >= start_hour or current_hour < end_hour
        else:
            # Regular schedule: allowed if current hour is within range
            return start_hour <= current_hour < end_hour

    def _normalize_schedule(self, schedule: dict) -> dict:
        """Normalize schedule to the array format, handling all legacy formats."""
        # Already in array format
        if isinstance(schedule.get("weekday"), list):
            return schedule

        # Single dict format: {"weekday": {"start_hour": 8, "end_hour": 22}}
        if isinstance(schedule.get("weekday"), dict):
            weekday = schedule["weekday"]
            weekend = schedule.get("weekend", weekday)
            return {
                "weekday": [weekday],
                "weekend": [weekend] if weekend != weekday else [weekday]
            }

        # Old flat format: {"start_hour": 8, "end_hour": 22, "weekday": true}
        if "start_hour" in schedule:
            window = {"start_hour": schedule["start_hour"], "end_hour": schedule["end_hour"]}
            return {
                "weekday": [window],
                "weekend": [window.copy()]
            }

        # Empty or unknown format
        return {"weekday": [], "weekend": []}

    def check_user_access_hours(self, username: str) -> bool:
        """Check if user is within allowed access hours

        Supports both regular hours (e.g., 8-22) and overnight hours (e.g., 22-6).
        Also supports separate weekday and weekend schedules with multiple time windows.
        A day type with no windows is unrestricted, so a user can be limited on
        weekdays while their weekend stays open.
        """
        try:
            daily_schedules = self.user_control_state.get("daily_schedules", {})
            if username not in daily_schedules:
                return True  # No restrictions

            schedule = daily_schedules[username]
            current_hour = datetime.now().hour
            day_of_week = datetime.now().weekday()  # 0=Monday, 6=Sunday

            # Determine if weekend (Saturday=5, Sunday=6)
            is_weekend = day_of_week >= 5

            # Normalize schedule to array format
            normalized = self._normalize_schedule(schedule)

            # Get appropriate windows based on day of week
            if is_weekend:
                windows = normalized.get("weekend", [])
            else:
                windows = normalized.get("weekday", [])

            # No windows for this day type means no restriction on it
            if not windows:
                return True

            # Check if current time falls within ANY of the windows
            for window in windows:
                start_hour = window.get("start_hour", 0)
                end_hour = window.get("end_hour", 24)
                if self._check_time_in_window(current_hour, start_hour, end_hour):
                    return True

            # Current time is not within any allowed window
            return False

        except Exception as e:
            self.logger.error(f"Error checking access hours for {username}: {e}")
            return True  # Default to allowing access on error
            
    def enforce_user_control(self):
        """Enforce account disables and access-hour schedules.

        Throttled to user_control.check_interval so that a user outside their
        allowed hours has their session ended periodically rather than having
        every process signalled on every monitoring pass.
        """
        settings = self.config.get("user_control", {})
        if not settings.get("enabled", False):
            return

        interval = settings.get("check_interval", 60)
        now = time.monotonic()
        if now - self.last_user_control_check < interval:
            return
        self.last_user_control_check = now

        self.user_control_state = self.load_user_control_state()

        # Snapshot keys: handle_disabled_user can re-enable and remove entries
        for username in list(self.user_control_state.get("disabled_users", {})):
            self.handle_disabled_user(username)

        for username in self.user_control_state.get("daily_schedules", {}):
            if username in self.user_control_state.get("disabled_users", {}):
                continue  # already handled above
            try:
                uid = pwd.getpwnam(username).pw_uid
            except KeyError:
                continue
            if not self.should_monitor_user(username, uid):
                continue
            if not self.check_user_access_hours(username):
                self.end_user_session(username, "outside allowed access hours")

    def monitor_processes(self, elapsed: float):
        """Run one monitoring pass, charging elapsed seconds against active limits"""
        self.enforce_user_control()

        to_terminate = []       # [(proc, reason)]
        tracked = []            # [(proc, username, limit_key, group_name)]
        active_users = set()
        limited_keys = {}       # {(username, key): limit_seconds}
        monitored_keys = set()  # {(username, key)}
        seen_groups = set()     # {(username, group)}
        limited_groups = {}     # {(username, group): limit_seconds}

        for proc in psutil.process_iter(['pid', 'name', 'uids']):
            try:
                pinfo = proc.info
                uids = pinfo['uids']
                process_name = pinfo['name']
                if uids is None or not process_name:
                    continue

                username = self.get_username(uids.real)
                if not username or not self.should_monitor_user(username, uids.real):
                    continue

                if self.is_process_blocked(process_name):
                    if not self.is_protected_process(proc):
                        to_terminate.append((proc, "Blocked application"))
                    continue

                group_name = self.get_process_group(process_name)
                limit_key, limit_seconds = self.get_process_limit(process_name)
                usage_key = limit_key or self.get_monitored_key(process_name)

                if not group_name and not usage_key:
                    continue

                active_users.add(username)
                if limit_key:
                    limited_keys[(username, limit_key)] = limit_seconds
                elif usage_key:
                    monitored_keys.add((username, usage_key))
                if group_name:
                    seen_groups.add((username, group_name))
                    group_limit = self.get_group_limit(group_name)
                    if group_limit:
                        limited_groups[(username, group_name)] = group_limit

                tracked.append((proc, username, limit_key, group_name))

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as e:
                self.logger.error(f"Error inspecting process: {e}")

        # Charge each user/key pair once per pass. Charging per process would
        # multiply usage by the process count, and a browser runs dozens.
        over_keys = set()
        for (username, key), limit in limited_keys.items():
            self.update_usage(username, key, elapsed)
            usage = self.get_usage(username, key)
            if usage >= limit:
                over_keys.add((username, key))
            else:
                self.maybe_warn_process(username, key, limit - usage)

        for username, key in monitored_keys - set(limited_keys):
            self.update_usage(username, key, elapsed)

        for username, group_name in seen_groups:
            self.update_group_usage(username, group_name, elapsed)

        over_groups = set()
        for (username, group_name), limit in limited_groups.items():
            usage = self.get_group_usage(username, group_name)
            if usage >= limit:
                over_groups.add((username, group_name))
            else:
                self.maybe_warn_group(username, group_name, limit - usage)

        for proc, username, limit_key, group_name in tracked:
            try:
                if self.is_protected_process(proc):
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if limit_key and (username, limit_key) in over_keys:
                limit = limited_keys[(username, limit_key)]
                to_terminate.append(
                    (proc, f"Daily limit for '{limit_key}' exceeded ({limit/60:.0f} minutes)"))
            elif group_name and (username, group_name) in over_groups:
                limit = limited_groups[(username, group_name)]
                to_terminate.append(
                    (proc, f"Group '{group_name}' daily limit exceeded ({limit/60:.0f} minutes)"))

        self.terminate_processes(to_terminate)

        for username in active_users:
            user_limit = self.get_user_daily_limit(username)
            if not user_limit:
                continue
            self.update_user_daily_usage(username, elapsed)
            usage = self.get_user_daily_usage(username)
            if usage >= user_limit:
                self.end_user_session(
                    username, f"daily screen time limit reached ({user_limit/60:.0f} minutes)")
            else:
                self.maybe_warn_user_daily(username, user_limit - usage)

    def log_usage_summary(self):
        """Log usage summary periodically"""
        if not self.daily_usage and not self.group_usage and not self.user_daily_usage:
            return

        self.logger.info("=== Usage Summary ===")

        # Log individual process usage
        if self.daily_usage:
            self.logger.info("Individual Process Usage:")
            for user, processes in self.daily_usage.items():
                for process, seconds in processes.items():
                    minutes = seconds / 60
                    _, limit = self.get_process_limit(process)
                    if limit:
                        remaining = max(0, (limit - seconds) / 60)
                        self.logger.info(f"  User: {user}, Process: {process}, Time: {minutes:.1f} minutes (Remaining: {remaining:.0f} minutes)")
                    else:
                        self.logger.info(f"  User: {user}, Process: {process}, Time: {minutes:.1f} minutes")

        # Log group usage
        if self.group_usage:
            self.logger.info("Group Usage:")
            for user, groups in self.group_usage.items():
                for group, seconds in groups.items():
                    minutes = seconds / 60
                    limit = self.get_group_limit(group)
                    if limit:
                        remaining = max(0, (limit - seconds) / 60)
                        self.logger.info(f"  User: {user}, Group: {group}, Time: {minutes:.1f} minutes (Remaining: {remaining:.0f} minutes)")
                    else:
                        self.logger.info(f"  User: {user}, Group: {group}, Time: {minutes:.1f} minutes")

        # Log user daily totals
        if self.user_daily_usage:
            self.logger.info("User Daily Screen Time:")
            for user, seconds in self.user_daily_usage.items():
                minutes = seconds / 60
                limit = self.get_user_daily_limit(user)
                if limit:
                    remaining = max(0, (limit - seconds) / 60)
                    self.logger.info(f"  User: {user}, Time: {minutes:.1f} minutes (Remaining: {remaining:.0f} minutes)")
                else:
                    self.logger.info(f"  User: {user}, Time: {minutes:.1f} minutes")

        self.logger.info("===================")
        
    def run(self):
        """Main monitoring loop"""
        self.logger.info("Child Minder started")

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

                if not self.config.get("enabled", True):
                    time.sleep(min(10, self.config.get('check_interval', 5)))
                    last_tick = time.monotonic()
                    continue

                self.reset_daily_usage()

                # Charge actual elapsed time rather than the configured interval,
                # which ignores how long each pass takes. The cap keeps a stall
                # or a resume from suspend from being counted as screen time.
                now = time.monotonic()
                elapsed = min(now - last_tick, self.config['check_interval'] * 2)
                last_tick = now

                self.monitor_processes(elapsed)

                if now - last_summary >= self.config.get('usage_log_interval', 60):
                    self.log_usage_summary()
                    last_summary = now

                if now - last_save >= 60:
                    self.save_state()
                    last_save = now

                time.sleep(self.config.get('check_interval', 5))

            except Exception as e:
                self.logger.exception(f"Error in main loop: {e}")
                time.sleep(5)
                last_tick = time.monotonic()

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

if __name__ == "__main__":
    main()
