"""Configuration schema, validation and lookups shared by the daemon and cmctl.

validate() is the single definition of a usable configuration. It returns a
canonical copy: every key present, every entry of the right type, and
user_daily_limits entries always in {"weekday": m, "weekend": m} form, where
either value may be None. A limit of 0 minutes means no time allowed. The one
exception to "right type" is monitored_users: an unusable value becomes None
(monitors nobody), because its [] default means every regular user.
"""

import copy
import fnmatch
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple

# UID_MIN and UID_MAX bound regular user accounts. Below sit root and system
# accounts; above sit nobody (65534) and systemd DynamicUser accounts
# (61184-65519).
LOGIN_DEFS_PATH = "/etc/login.defs"
DEFAULT_UID_MIN = 1000
DEFAULT_UID_MAX = 60000

DAY_TYPES = ("weekday", "weekend")

Warn = Callable[[str], None]


class ConfigError(Exception):
    """Raised when a configuration file cannot be read or parsed"""


def defaults() -> dict:
    """Return the default configuration"""
    return {
        "enabled": True,
        "check_interval": 5,
        "min_uid": DEFAULT_UID_MIN,
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


def read_uid_range(path: str = LOGIN_DEFS_PATH) -> Tuple[int, int]:
    """Return (UID_MIN, UID_MAX) for regular users, from login.defs where it sets them"""
    found = {"UID_MIN": DEFAULT_UID_MIN, "UID_MAX": DEFAULT_UID_MAX}
    try:
        with open(path) as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2 and fields[0] in found:
                    try:
                        found[fields[0]] = int(fields[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return found["UID_MIN"], found["UID_MAX"]


def is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def is_minutes(value) -> bool:
    """Whether value is a usable limit in minutes; 0 is valid and allows no time"""
    return is_number(value) and value >= 0


def read_file(path: Path) -> dict:
    """Read a config file as a JSON object, raising ConfigError if that is impossible"""
    try:
        with open(path, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found at {path}")
    except PermissionError:
        raise ConfigError(f"Permission denied reading {path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {path}: {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {path}: {e}")
    if not isinstance(config, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return config


def load(path: Path, warn: Warn, uid_min: int) -> dict:
    """Read and validate a config file, raising ConfigError if it is unusable"""
    return validate(read_file(path), warn, uid_min)


def validate(raw: dict, warn: Warn, uid_min: int) -> dict:
    """Return a canonical copy of raw, replacing or dropping each invalid entry.

    Only the bad entry goes, with a warning, so one typo in a hand-edited file
    leaves the rest of the configuration enforced. min_uid may not go below
    uid_min: nothing done as root may target a system account.
    """
    config = copy.deepcopy(raw)
    base = defaults()

    for key, default_value in base.items():
        if key == 'monitored_users':
            continue  # handled below: its default means "every user"
        if not _same_kind(config.setdefault(key, default_value), default_value):
            warn(f"Invalid '{key}', using default")
            config[key] = default_value

    # An empty monitored_users list deliberately means "every regular user",
    # so an unusable value must not fall back to it: that would widen a typo
    # to accounts the administrator never named. None monitors nobody.
    raw_users = raw.get('monitored_users', [])
    if isinstance(raw_users, list):
        users = [u for u in raw_users if isinstance(u, str)]
        if len(users) != len(raw_users):
            warn("Ignoring non-string entries in 'monitored_users'")
        config['monitored_users'] = users if users or not raw_users else None
    else:
        warn("Invalid 'monitored_users', monitoring no users")
        config['monitored_users'] = None

    if config['check_interval'] <= 0:
        warn("Invalid check_interval, using default: 5")
        config['check_interval'] = 5

    if not isinstance(config['min_uid'], int) or config['min_uid'] < uid_min:
        warn(f"min_uid must be a whole number of at least UID_MIN ({uid_min}); using {uid_min}")
        config['min_uid'] = uid_min

    for key in ('warning_time', 'usage_log_interval'):
        if config[key] <= 0:
            warn(f"Invalid {key}, using default: {base[key]}")
            config[key] = base[key]

    control = config['user_control']
    for key, default_value in base['user_control'].items():
        control.setdefault(key, default_value)
    if not isinstance(control['enabled'], bool):
        warn("Invalid user_control enabled, using default: false")
        control['enabled'] = False
    if not is_number(control['check_interval']) or control['check_interval'] <= 0:
        warn("Invalid user_control check_interval, using default: 60")
        control['check_interval'] = 60

    for key in ('blocked_processes', 'monitored_processes'):
        config[key] = _valid_patterns(key, config[key], warn)

    for group, patterns in list(config['process_groups'].items()):
        if isinstance(patterns, list):
            config['process_groups'][group] = _valid_patterns(
                f"process_groups.{group}", patterns, warn)
        else:
            warn(f"Ignoring invalid process_groups entry '{group}'")
            del config['process_groups'][group]

    for key in ('limited_processes', 'group_limits'):
        for name, minutes in list(config[key].items()):
            if not is_minutes(minutes):
                warn(f"Ignoring invalid {key} entry '{name}'")
                del config[key][name]

    for user, entry in list(config['user_daily_limits'].items()):
        if is_minutes(entry):
            config['user_daily_limits'][user] = {day: entry for day in DAY_TYPES}
        elif isinstance(entry, dict) and all(
                entry.get(day) is None or is_minutes(entry.get(day)) for day in DAY_TYPES):
            config['user_daily_limits'][user] = {day: entry.get(day) for day in DAY_TYPES}
        else:
            warn(f"Ignoring invalid user_daily_limits entry '{user}'")
            del config['user_daily_limits'][user]

    intervals = config.get('warning_intervals')
    if intervals is not None and not (
            isinstance(intervals, list) and all(is_number(w) and w > 0 for w in intervals)):
        warn("Invalid warning_intervals, using warning_time")
        del config['warning_intervals']

    return config


def _same_kind(value, default) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if is_number(default):
        return is_number(value)
    return isinstance(value, type(default))


def _valid_patterns(name: str, patterns: list, warn: Warn) -> list:
    kept = [p for p in patterns if isinstance(p, str)]
    if len(kept) != len(patterns):
        warn(f"Ignoring non-string patterns in '{name}'")
    return kept


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


def match_pattern(patterns, process_name: str) -> Optional[str]:
    """Return the first pattern from patterns matching process_name, if any"""
    for pattern in patterns:
        if pattern_matches(pattern, process_name):
            return pattern
    return None


def day_type(moment: datetime) -> str:
    """Return "weekend" for Saturday and Sunday, "weekday" otherwise"""
    return "weekend" if moment.weekday() >= 5 else "weekday"


def user_daily_limit(config: dict, username: str, moment: datetime) -> Optional[float]:
    """Return username's screen time limit in minutes for moment's day type, if any"""
    entry = config["user_daily_limits"].get(username)
    if entry is None:
        return None
    return entry[day_type(moment)]
