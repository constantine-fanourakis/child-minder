"""Usage accounting and limit decisions for a monitoring pass, free of side effects.

evaluate() takes the processes the daemon scanned, charges elapsed time to the
usage counters, and returns what should happen: processes to terminate,
warnings to give and sessions to end. The daemon carries those out.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Hashable, List, Optional, Set, Tuple

from childminder import config as cfg


@dataclass
class Process:
    handle: object      # what the daemon acts on; opaque here
    name: str
    username: str
    protected: bool     # the daemon itself, its children or PID 1


@dataclass
class LimitWarning:
    kind: str           # "process", "group" or "daily"
    username: str
    subject: str        # the application for "process", the group for "group"
    key: str            # the config key the limit belongs to
    remaining_seconds: float

    @property
    def minutes(self) -> int:
        """Remaining time rounded up to whole minutes, for display"""
        return max(0, math.ceil(self.remaining_seconds / 60))


@dataclass
class Actions:
    terminate: List[Tuple[object, str]] = field(default_factory=list)
    warnings: List[LimitWarning] = field(default_factory=list)
    end_sessions: List[Tuple[str, str]] = field(default_factory=list)


class Usage:
    """A day's usage in seconds, and the warning thresholds already used.

    Process usage is kept under the config key rather than the observed
    process name, so that helper processes with different names (chrome and
    chrome_crashpad_handler, say) share one counter with the limit.
    """

    def __init__(self, processes: Dict[str, Dict[str, float]] = None,
                 groups: Dict[str, Dict[str, float]] = None,
                 daily: Dict[str, float] = None):
        self.processes = processes or {}
        self.groups = groups or {}
        self.daily = daily or {}
        # Thresholds used, keyed by the limit as well as what it applies to, so
        # a limit changed by a config reload starts with every warning unused
        self.warned: Dict[Hashable, Set[float]] = {}

    @classmethod
    def from_state(cls, state: dict, warn: Optional[Callable[[str], None]] = None) -> "Usage":
        """Restore usage saved by to_state, dropping anything that is not a counter"""
        dropped = []

        def counters(value) -> dict:
            if not isinstance(value, dict):
                return {}
            kept = {key: float(seconds) for key, seconds in value.items()
                    if cfg.is_number(seconds)}
            if len(kept) != len(value):
                dropped.append(True)
            return kept

        def table(value) -> dict:
            if not isinstance(value, dict):
                return {}
            return {user: counters(keys) for user, keys in value.items()
                    if isinstance(keys, dict)}

        usage = cls(table(state.get("daily_usage")), table(state.get("group_usage")),
                    counters(state.get("user_daily_usage")))
        if dropped and warn:
            warn("Ignoring usage counters in the state file that are not numbers")
        return usage

    def to_state(self) -> dict:
        return {"daily_usage": self.processes, "group_usage": self.groups,
                "user_daily_usage": self.daily}

    def is_empty(self) -> bool:
        return not (self.processes or self.groups or self.daily)

    def charge_process(self, username: str, key: str, seconds: float):
        user = self.processes.setdefault(username, {})
        user[key] = user.get(key, 0) + seconds

    def process_seconds(self, username: str, key: str) -> float:
        return self.processes.get(username, {}).get(key, 0)

    def charge_group(self, username: str, group: str, seconds: float):
        user = self.groups.setdefault(username, {})
        user[group] = user.get(group, 0) + seconds

    def group_seconds(self, username: str, group: str) -> float:
        return self.groups.get(username, {}).get(group, 0)

    def charge_daily(self, username: str, seconds: float):
        self.daily[username] = self.daily.get(username, 0) + seconds

    def daily_seconds(self, username: str) -> float:
        return self.daily.get(username, 0)

    def consume_warning(self, limit: Hashable, remaining_seconds: float,
                        intervals: List[float]) -> Optional[float]:
        """Return the largest not-yet-used threshold that remaining_seconds has crossed.

        Every crossed threshold is marked used, not just the one returned. A
        process started with little quota left crosses several thresholds at
        once; marking only one would emit a further warning on each following
        pass until the list was exhausted.
        """
        consumed = self.warned.setdefault(limit, set())
        crossed = [w for w in intervals if remaining_seconds <= w]
        fresh = [w for w in crossed if w not in consumed]
        consumed.update(crossed)
        return max(fresh) if fresh else None


def warning_intervals(config: dict) -> List[float]:
    """Warning thresholds in seconds, largest first"""
    intervals = config.get('warning_intervals') or [config['warning_time']]
    return sorted(intervals, reverse=True)


def process_limit(config: dict, process_name: str) -> Tuple[Optional[str], Optional[float]]:
    """Return (config key, limit in seconds) for a limited process, or (None, None)"""
    for pattern, minutes in config["limited_processes"].items():
        if cfg.pattern_matches(pattern, process_name):
            return pattern, minutes * 60
    return None, None


def process_limit_by_key(config: dict, key: str) -> Optional[float]:
    """Return the limit in seconds of a usage key, if it is a limited_processes pattern"""
    minutes = config["limited_processes"].get(key)
    return None if minutes is None else minutes * 60


def process_group(config: dict, process_name: str) -> Optional[str]:
    """Return the group of a process; one listed in several belongs to the first in config order"""
    for group, patterns in config["process_groups"].items():
        if cfg.match_pattern(patterns, process_name):
            return group
    return None


def group_limit(config: dict, group: str) -> Optional[float]:
    """Return a group's limit in seconds, if it has one"""
    minutes = config["group_limits"].get(group)
    return None if minutes is None else minutes * 60


def daily_limit(config: dict, username: str, now: datetime) -> Optional[float]:
    """Return a user's screen time limit in seconds for now's day type, if any"""
    minutes = cfg.user_daily_limit(config, username, now)
    return None if minutes is None else minutes * 60


@dataclass
class UsageLine:
    username: str
    subject: str                    # the config key or group; "" for daily screen time
    seconds: float
    limit_seconds: Optional[float]

    @property
    def remaining_seconds(self) -> Optional[float]:
        if self.limit_seconds is None:
            return None
        return max(0.0, self.limit_seconds - self.seconds)


def usage_report(usage: Usage, config: dict, now: datetime) -> Dict[str, List[UsageLine]]:
    """Pair each usage counter with the limit it counts against, by kind"""
    return {
        "process": [UsageLine(user, key, seconds, process_limit_by_key(config, key))
                    for user, keys in usage.processes.items()
                    for key, seconds in keys.items()],
        "group": [UsageLine(user, group, seconds, group_limit(config, group))
                  for user, groups in usage.groups.items()
                  for group, seconds in groups.items()],
        "daily": [UsageLine(user, "", seconds, daily_limit(config, user, now))
                  for user, seconds in usage.daily.items()],
    }


def evaluate(processes: List[Process], config: dict, usage: Usage,
             elapsed: float, now: datetime) -> Actions:
    """Charge elapsed seconds against active limits and decide what to enforce.

    Each user/key pair is charged once per pass, however many processes it
    has: charging per process would multiply usage by the process count, and
    a browser runs dozens.
    """
    actions = Actions()
    intervals = warning_intervals(config)
    tracked = []            # [(process, limit_key, group)]
    active_users = set()
    limited_keys = {}       # {(username, key): limit_seconds}
    app_names = {}          # {(username, key): first process name seen}
    monitored_keys = set()  # {(username, key)}
    seen_groups = set()     # {(username, group)}
    limited_groups = {}     # {(username, group): limit_seconds}

    for proc in processes:
        if cfg.match_pattern(config["blocked_processes"], proc.name):
            if not proc.protected:
                actions.terminate.append((proc.handle, "Blocked application"))
            continue

        group = process_group(config, proc.name)
        limit_key, limit_seconds = process_limit(config, proc.name)
        usage_key = limit_key or cfg.match_pattern(config["monitored_processes"], proc.name)
        if not group and not usage_key:
            continue

        active_users.add(proc.username)
        if limit_key:
            limited_keys[(proc.username, limit_key)] = limit_seconds
            app_names.setdefault((proc.username, limit_key), proc.name)
        elif usage_key:
            monitored_keys.add((proc.username, usage_key))
        if group:
            seen_groups.add((proc.username, group))
            limit = group_limit(config, group)
            if limit is not None:
                limited_groups[(proc.username, group)] = limit
        tracked.append((proc, limit_key, group))

    over_keys = set()
    for (username, key), limit in limited_keys.items():
        usage.charge_process(username, key, elapsed)
        remaining = limit - usage.process_seconds(username, key)
        if remaining <= 0:
            over_keys.add((username, key))
        elif usage.consume_warning(("process", username, key, limit), remaining,
                                   intervals) is not None:
            actions.warnings.append(LimitWarning("process", username, app_names[(username, key)],
                                            key, remaining))

    for username, key in monitored_keys - set(limited_keys):
        usage.charge_process(username, key, elapsed)

    for username, group in seen_groups:
        usage.charge_group(username, group, elapsed)

    over_groups = set()
    for (username, group), limit in limited_groups.items():
        remaining = limit - usage.group_seconds(username, group)
        if remaining <= 0:
            over_groups.add((username, group))
        elif usage.consume_warning(("group", username, group, limit), remaining,
                                   intervals) is not None:
            actions.warnings.append(LimitWarning("group", username, group, group, remaining))

    for proc, limit_key, group in tracked:
        if proc.protected:
            continue
        if limit_key and (proc.username, limit_key) in over_keys:
            minutes = limited_keys[(proc.username, limit_key)] / 60
            actions.terminate.append(
                (proc.handle, f"Daily limit for '{limit_key}' exceeded ({minutes:.0f} minutes)"))
        elif group and (proc.username, group) in over_groups:
            minutes = limited_groups[(proc.username, group)] / 60
            actions.terminate.append(
                (proc.handle, f"Group '{group}' daily limit exceeded ({minutes:.0f} minutes)"))

    for username in sorted(active_users):
        # Track screen time even on unlimited days, so a limit added later
        # applies to the time already used and the dashboard stays accurate.
        usage.charge_daily(username, elapsed)
        limit = daily_limit(config, username, now)
        if limit is None:
            continue
        remaining = limit - usage.daily_seconds(username)
        if remaining <= 0:
            actions.end_sessions.append(
                (username, f"daily screen time limit reached ({limit / 60:.0f} minutes)"))
        elif usage.consume_warning(("daily", username, limit), remaining,
                                   intervals) is not None:
            actions.warnings.append(LimitWarning("daily", username, "", "", remaining))

    return actions
