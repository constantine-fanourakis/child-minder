#!/usr/bin/env python3
"""
Child Minder - Process Monitor and Control System
A parental control system for monitoring and limiting application usage on Linux
"""

import os
import time
import json
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

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# How long a resolved DISPLAY stays usable before it is looked up again
DISPLAY_CACHE_TTL = 300
NOTIFY_TIMEOUT = 5
COMMAND_TIMEOUT = 10

SOUND_FILES = [
    '/usr/share/sounds/freedesktop/stereo/dialog-warning.oga',
    '/usr/share/sounds/ubuntu/stereo/dialog-warning.ogg',
    '/usr/share/sounds/gnome/default/alerts/glass.ogg',
]

# Copy of the last config that parsed, kept beside the state file and used when
# the live config becomes unreadable
LAST_GOOD_CONFIG_NAME = "config.last-good.json"


class ConfigError(Exception):
    """Raised when a configuration file cannot be read or parsed"""


class ChildMinder:
    def __init__(self, config_path: str, state_path: str, log_path: str):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        
        # Ensure directories exist
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.last_good_config_path = self.state_path.parent / LAST_GOOD_CONFIG_NAME
        
        # Setup logging
        self.setup_logging()
        
        # Load configuration
        self.config = self.load_config()
        
        # Load or initialize state
        self.state = self.load_state()
        self.daily_usage: Dict[str, Dict[str, float]] = {}
        self.deserialize_usage(self.state.get("daily_usage", {}))
        self.group_usage: Dict[str, Dict[str, float]] = self.state.get("group_usage", {})
        self.user_daily_usage: Dict[str, float] = self.state.get("user_daily_usage", {})
        self.warned_users: Dict[str, Set[int]] = {}    # {username: set of warning thresholds sent}

        self.display_cache: Dict[str, Tuple[str, float]] = {}

        # Track which warnings have been sent
        self.warned_processes: Dict[int, Set[int]] = {}  # pid -> set of warning times
        self.warned_groups: Dict[str, Dict[str, Set[int]]] = {}  # group -> user -> warning times
        
        # User control state file
        self.user_control_file = Path("/var/lib/child-minder/user_control.json")
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
                return self.validate_config(json.load(f))
        except FileNotFoundError:
            raise ConfigError(f"Config file not found at {path}")
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {path}: {e}")
        except OSError as e:
            raise ConfigError(f"Could not read {path}: {e}")

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
                              ('user_daily_limits', dict)):
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
                "check_interval": 60,
                "auto_disable_on_violations": False,
                "violation_threshold": 3
            }
        }
        
    def load_state(self) -> dict:
        """Load saved state from file"""
        try:
            if self.state_path.exists():
                with open(self.state_path, 'r') as f:
                    state = json.load(f)
                    # Load group usage if present
                    if "group_usage" in state:
                        self.group_usage = state["group_usage"]
                    # Load user daily usage if present
                    if "user_daily_usage" in state:
                        self.user_daily_usage = state["user_daily_usage"]
                    return state
        except Exception as e:
            self.logger.warning(f"Could not load state: {e}")
        
        return {
            "daily_usage": {},
            "group_usage": {},
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
        self.warned_processes = {}
        self.warned_groups = {}
        self.warned_users = {}
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
        """
        if uid < self.config.get("min_uid", 1000):
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
    
    def should_warn(self, remaining_seconds: float, pid: Optional[int] = None, 
                    group_name: Optional[str] = None, username: Optional[str] = None) -> Optional[int]:
        """
        Check if a warning should be sent based on remaining time
        Returns the warning interval in seconds if warning should be sent, None otherwise
        """
        # Get warning intervals (in seconds)
        warning_intervals = self.config.get('warning_intervals', [])
        if not warning_intervals:
            # Fall back to single warning_time if no intervals defined
            warning_time = self.config.get('warning_time', 300)
            warning_intervals = [warning_time]
        
        for warning_seconds in sorted(warning_intervals):
            # Check if we're within this warning threshold
            if remaining_seconds <= warning_seconds:
                # Check if we've already warned at this level
                if pid is not None:
                    # Process warning
                    if pid not in self.warned_processes:
                        self.warned_processes[pid] = set()
                    if warning_seconds not in self.warned_processes[pid]:
                        self.warned_processes[pid].add(warning_seconds)
                        return warning_seconds
                elif group_name and username:
                    # Group warning
                    if group_name not in self.warned_groups:
                        self.warned_groups[group_name] = {}
                    if username not in self.warned_groups[group_name]:
                        self.warned_groups[group_name][username] = set()
                    if warning_seconds not in self.warned_groups[group_name][username]:
                        self.warned_groups[group_name][username].add(warning_seconds)
                        return warning_seconds
        
        return None
    
    def warn_user_group(self, username: str, group_name: str, remaining_minutes: int):
        """Send warning to user about group time limit"""
        try:
            # Ensure remaining_minutes is non-negative
            remaining_minutes = max(0, remaining_minutes)

            # Determine urgency based on time
            urgency = 'critical' if remaining_minutes <= 5 else 'normal'
            title = "Group Time Limit Warning" if remaining_minutes > 1 else "FINAL GROUP WARNING"

            # Build message
            message = (f"Group '{group_name}' has {remaining_minutes} minute{'s' if remaining_minutes > 1 else ''} remaining today. "
                      f"All apps in this group will close when time expires.")
            if remaining_minutes <= 2:
                message += " SAVE YOUR WORK NOW!"

            # Send notification using safe method
            play_sound = remaining_minutes <= 5
            success = self.send_notification(username, title, message, urgency, play_sound)

            # Wall message for critical warnings
            if remaining_minutes <= 2:
                try:
                    subprocess.run(['wall', f"GROUP WARNING: {message}"], capture_output=True, timeout=2)
                    success = True
                except subprocess.TimeoutExpired:
                    pass

            if success:
                self.logger.info(f"Warned user {username} about group '{group_name}' time limit ({remaining_minutes} min remaining)")
            else:
                self.logger.warning(f"Could not send desktop notification for {username}, using wall")
                subprocess.run(['wall', f"GROUP WARNING: {group_name} has {remaining_minutes} minutes left"],
                             capture_output=True, timeout=2)

        except Exception as e:
            self.logger.error(f"Error in group warning system: {e}")
    
    def get_process_limit(self, process_name: str) -> Optional[int]:
        """Get time limit for process in seconds"""
        for limited_name, limit_minutes in self.config.get("limited_processes", {}).items():
            if self.pattern_matches(limited_name, process_name):
                return limit_minutes * 60  # Convert to seconds
        return None

    def is_process_monitored(self, process_name: str) -> bool:
        """Check if process should be logged"""
        return self.match_pattern(self.config.get("monitored_processes", []), process_name) is not None

        
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
        
    def terminate_process(self, proc: psutil.Process, reason: str):
        """Terminate a process and log the action"""
        try:
            process_name = proc.name()
            pid = proc.pid
            username = self.get_username(proc.uids().real)
            
            # Try graceful termination first
            proc.terminate()
            time.sleep(1)
            
            # Force kill if still running
            if proc.is_running():
                proc.kill()
                
            self.logger.warning(f"Terminated process: {process_name} (PID: {pid}, User: {username}) - Reason: {reason}")
            
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self.logger.debug(f"Could not terminate process: {e}")
            
    def warn_user(self, proc: psutil.Process, remaining_minutes: int):
        """Send warning to user about time limit"""
        try:
            username = self.get_username(proc.uids().real)
            process_name = proc.name()

            # Ensure remaining_minutes is non-negative
            remaining_minutes = max(0, remaining_minutes)

            # Determine urgency based on time
            urgency = 'critical' if remaining_minutes <= 5 else 'normal'
            title = "Time Limit Warning" if remaining_minutes > 1 else "FINAL WARNING"

            # Build message
            message = f"{process_name} has {remaining_minutes} minute{'s' if remaining_minutes > 1 else ''} remaining today."
            if remaining_minutes <= 2:
                message += " SAVE YOUR WORK NOW!"

            # Send notification using safe method
            play_sound = remaining_minutes <= 5
            success = self.send_notification(username, title, message, urgency, play_sound)

            # Wall message for critical warnings
            if remaining_minutes <= 2:
                try:
                    subprocess.run(['wall', f"WARNING: {message}"], capture_output=True, timeout=2)
                    success = True
                except subprocess.TimeoutExpired:
                    pass

            # Terminal message for active terminals (critical warnings only)
            if remaining_minutes <= 5:
                try:
                    for proc_iter in psutil.process_iter(['username', 'terminal']):
                        if proc_iter.username() == username and proc_iter.info['terminal']:
                            try:
                                tty = f"/dev/{proc_iter.info['terminal']}"
                                with open(tty, 'w') as t:
                                    t.write(f"\n*** {title} ***\n{message}\n\n")
                                success = True
                            except (IOError, OSError, PermissionError):
                                continue
                except Exception:
                    pass

            if success:
                self.logger.info(f"Warned user {username} about {process_name} time limit ({remaining_minutes} min remaining)")
            else:
                self.logger.warning(f"Could not send desktop notification for {username}, using wall")
                subprocess.run(['wall', f"TIME WARNING: {process_name} has {remaining_minutes} minutes left"],
                             capture_output=True, timeout=2)

        except Exception as e:
            self.logger.error(f"Error in warning system: {e}")
    
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
            "scheduled_disables": {},
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

    def should_warn_user(self, username: str, remaining_seconds: float) -> Optional[int]:
        """Check if a daily-limit warning should be sent to a user"""
        warning_intervals = self.config.get('warning_intervals', [self.config.get('warning_time', 300)])
        for w in sorted(warning_intervals):
            if remaining_seconds <= w:
                self.warned_users.setdefault(username, set())
                if w not in self.warned_users[username]:
                    self.warned_users[username].add(w)
                    return w
        return None

    def warn_user_daily_limit(self, username: str, remaining_minutes: int):
        """Send warning to user about their overall daily screen time limit"""
        try:
            remaining_minutes = max(0, remaining_minutes)
            urgency = 'critical' if remaining_minutes <= 5 else 'normal'
            title = "Daily Screen Time Warning" if remaining_minutes > 1 else "FINAL SCREEN TIME WARNING"
            message = (f"You have {remaining_minutes} minute{'s' if remaining_minutes != 1 else ''} "
                       f"of screen time remaining today. All apps will close when time expires.")
            if remaining_minutes <= 2:
                message += " SAVE YOUR WORK NOW!"
            play_sound = remaining_minutes <= 5
            self.send_notification(username, title, message, urgency, play_sound)
            if remaining_minutes <= 2:
                subprocess.run(['wall', f"SCREEN TIME: {message}"], capture_output=True, timeout=2)
            self.logger.info(f"Warned {username} about daily limit ({remaining_minutes} min remaining)")
        except Exception as e:
            self.logger.error(f"Error in user daily limit warning: {e}")

    def kill_user_processes(self, username: str):
        """Kill all processes for a user"""
        try:
            killed_count = 0
            for proc in psutil.process_iter(['username', 'pid', 'name']):
                try:
                    if proc.username() == username:
                        proc.terminate()
                        killed_count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if killed_count > 0:
                self.logger.info(f"Terminated {killed_count} processes for user {username}")

        except Exception as e:
            self.logger.error(f"Error killing processes for user {username}: {e}")

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

            # User is still disabled, kill their processes
            self.kill_user_processes(username)
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

            self.logger.info(f"User account re-enabled: {username}")

        except Exception as e:
            self.logger.error(f"Error enabling user account {username}: {e}")

    def save_user_control_state(self):
        """Save the user control state to file"""
        try:
            self.user_control_file.parent.mkdir(parents=True, exist_ok=True)
            self.atomic_write_json(self.user_control_file, self.user_control_state)
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

            # If no windows defined for this day, deny access
            # (User has a schedule entry, so parental controls are active)
            if not windows:
                return False

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
            
    def monitor_processes(self):
        """Main monitoring loop iteration"""
        # First, check for disabled users if user control is enabled
        if self.config.get("user_control", {}).get("enabled", False):
            # Reload user control state
            self.user_control_state = self.load_user_control_state()
            
            # Handle any disabled users (snapshot keys to avoid mutation during iteration)
            for username in list(self.user_control_state.get("disabled_users", {})):
                self.handle_disabled_user(username)
            
            # Check access hours for all monitored users
            for username in self.config.get("monitored_users", []):
                if not self.check_user_access_hours(username):
                    # User is outside allowed hours, kill their processes
                    self.kill_user_processes(username)
        
        # Regular process monitoring
        active_users: set = set()
        for proc in psutil.process_iter(['pid', 'name', 'username', 'uids']):
            try:
                # Get process info
                pinfo = proc.info
                
                uids = pinfo['uids']
                if uids is None:  # kernel thread
                    continue

                username = self.get_username(uids.real)
                if not username or not self.should_monitor_user(username, uids.real):
                    continue

                process_name = pinfo['name']

                # Check if process is blocked
                if self.is_process_blocked(process_name) and not self.is_protected_process(proc):
                    self.terminate_process(proc, "Blocked application")
                    continue
                
                # Check which group this process belongs to, if any
                group_name = self.get_process_group(process_name)

                # Track for user daily limit (only count limited/monitored processes)
                if (group_name or self.get_process_limit(process_name) or self.is_process_monitored(process_name)):
                    active_users.add(username)

                # Track whether to terminate due to limits
                should_terminate = False
                terminate_reason = ""

                # Track whether usage was already updated (to avoid double counting)
                usage_updated = False
                group_usage_updated = False

                # Check group limits first (if applicable)
                if group_name:
                    group_limit = self.get_group_limit(group_name)
                    if group_limit:
                        # Update group usage
                        self.update_group_usage(username, group_name, self.config['check_interval'])
                        group_usage_updated = True
                        current_group_usage = self.get_group_usage(username, group_name)

                        if current_group_usage >= group_limit:
                            should_terminate = True
                            terminate_reason = f"Group '{group_name}' daily limit exceeded ({group_limit/60:.0f} minutes)"
                        else:
                            # Check if we should warn
                            remaining_seconds = group_limit - current_group_usage
                            warning_level = self.should_warn(remaining_seconds,
                                                            group_name=group_name,
                                                            username=username)
                            if warning_level:
                                remaining_minutes = int(remaining_seconds / 60)
                                self.warn_user_group(username, group_name, remaining_minutes)

                # Check individual time limits
                individual_limit = self.get_process_limit(process_name)
                if individual_limit:
                    # Update individual usage
                    self.update_usage(username, process_name, self.config['check_interval'])
                    usage_updated = True
                    current_usage = self.get_usage(username, process_name)

                    if current_usage >= individual_limit:
                        should_terminate = True
                        terminate_reason = f"Individual daily time limit exceeded ({individual_limit/60:.0f} minutes)"
                    else:
                        # Check if we should warn
                        remaining_seconds = individual_limit - current_usage
                        warning_level = self.should_warn(remaining_seconds, pid=proc.pid)
                        if warning_level:
                            remaining_minutes = int(remaining_seconds / 60)
                            self.warn_user(proc, remaining_minutes)

                # Terminate if any limit exceeded
                if should_terminate:
                    self.terminate_process(proc, terminate_reason)
                    continue

                # Log monitored processes (only update if not already updated by limits)
                if self.is_process_monitored(process_name):
                    if not usage_updated:
                        self.update_usage(username, process_name, self.config['check_interval'])
                    # Also update group usage for monitoring if not already updated
                    if group_name and not group_usage_updated:
                        self.update_group_usage(username, group_name, self.config['check_interval'])
                    
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as e:
                self.logger.error(f"Error monitoring process: {e}")

        # Check user daily limits
        for username in active_users:
            user_limit = self.get_user_daily_limit(username)
            if user_limit:
                self.update_user_daily_usage(username, self.config['check_interval'])
                current = self.get_user_daily_usage(username)
                if current >= user_limit:
                    self.logger.warning(
                        f"User {username} daily screen time limit exceeded ({user_limit/60:.0f} min), killing processes"
                    )
                    self.kill_user_processes(username)
                else:
                    remaining = user_limit - current
                    if self.should_warn_user(username, remaining):
                        self.warn_user_daily_limit(username, int(remaining / 60))

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
        
    def run(self):
        """Main monitoring loop"""
        self.logger.info("Child Minder started")

        last_summary = time.monotonic()
        last_save = time.monotonic()

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
                    continue

                self.reset_daily_usage()
                self.monitor_processes()

                now = time.monotonic()
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
