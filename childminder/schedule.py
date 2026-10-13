"""Access schedules: format, evaluation and editing, shared by the daemon and cmctl.

A schedule has a list of windows for each day type, and an optional no_access
list naming day types that are denied all day:

    {"weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": [],
     "no_access": ["weekend"]}

A window runs from start_hour up to but not including end_hour, and wraps past
midnight when start_hour is the larger. A day type with no windows that is not
denied is unrestricted. no_access is present only when it names a day type.
"""

from datetime import datetime
from typing import Callable, Optional

from childminder.config import DAY_TYPES, day_type

DAY_LABELS = {"weekday": "Weekday (Mon-Fri)", "weekend": "Weekend (Sat-Sun)"}

Warn = Callable[[str], None]


def empty() -> dict:
    return {"weekday": [], "weekend": []}


def hours_error(start_hour: int, end_hour: int) -> Optional[str]:
    """Return why start_hour..end_hour is not a valid new window, or None"""
    if not 0 <= start_hour <= 23:
        return f"Start hour must be between 0 and 23, got {start_hour}"
    if not 0 <= end_hour <= 23:
        return f"End hour must be between 0 and 23, got {end_hour}"
    if start_hour == end_hour:
        return "Start and end hours cannot be the same"
    return None


def _is_window(window) -> bool:
    if not isinstance(window, dict):
        return False
    start, end = window.get("start_hour"), window.get("end_hour")
    return (isinstance(start, int) and not isinstance(start, bool) and 0 <= start <= 23
            and isinstance(end, int) and not isinstance(end, bool) and 0 <= end <= 24
            and start != end)


def normalize(raw, warn: Optional[Warn] = None) -> dict:
    """Return a schedule in the current format from any format cmctl has written.

    Malformed windows and unknown no_access entries are dropped, with a
    warning when warn is given. A day type whose entry holds windows but
    none that can be read is denied rather than left unrestricted, so a
    damaged schedule never opens access.
    """
    if not isinstance(raw, dict):
        raw = {}

    if isinstance(raw.get("weekday"), dict):
        # {"weekday": {...}, "weekend": {...}}, one window per day type
        weekday = raw["weekday"]
        days = {"weekday": [weekday], "weekend": [raw.get("weekend", weekday)]}
    elif "start_hour" in raw:
        # {"start_hour": 8, "end_hour": 22}, one window for every day
        window = {"start_hour": raw.get("start_hour"), "end_hour": raw.get("end_hour")}
        days = {"weekday": [window], "weekend": [dict(window)]}
    else:
        # A present entry that is not a list stands for one unreadable window
        days = {day: raw.get(day) if isinstance(raw.get(day), list)
                else [] if raw.get(day) is None else [raw.get(day)]
                for day in DAY_TYPES}

    schedule = {}
    unreadable = []
    for day in DAY_TYPES:
        windows = [{"start_hour": w["start_hour"], "end_hour": w["end_hour"]}
                   for w in days[day] if _is_window(w)]
        if len(windows) != len(days[day]):
            if not windows:
                unreadable.append(day)
            if warn:
                warn(f"Ignoring malformed {day} windows"
                     + ("; denying that day type" if not windows else ""))
        schedule[day] = windows

    denied = raw.get("no_access", [])
    if not isinstance(denied, list):
        denied = []
    denied = [day for day in DAY_TYPES if day in denied or day in unreadable]
    if denied:
        schedule["no_access"] = denied
    return schedule


def in_window(hour: int, window: dict) -> bool:
    start, end = window["start_hour"], window["end_hour"]
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def is_allowed(schedule: dict, moment: datetime) -> bool:
    """Whether a normalized schedule allows access at moment"""
    day = day_type(moment)
    if is_denied(schedule, day):
        return False
    windows = schedule[day]
    return not windows or any(in_window(moment.hour, w) for w in windows)


def is_denied(schedule: dict, day: str) -> bool:
    return day in schedule.get("no_access", [])


def is_unrestricted(schedule: dict, day: str) -> bool:
    return not schedule[day] and not is_denied(schedule, day)


def allow(schedule: dict, day: str):
    """Remove a day type's no-access mark, dropping the key once nothing is denied"""
    denied = [d for d in schedule.get("no_access", []) if d != day]
    if denied:
        schedule["no_access"] = denied
    else:
        schedule.pop("no_access", None)


def deny(schedule: dict, day: str):
    """Deny all access on a day type, replacing any windows it had"""
    schedule[day] = []
    if not is_denied(schedule, day):
        schedule["no_access"] = [d for d in DAY_TYPES
                                 if d == day or is_denied(schedule, d)]


def set_window(schedule: dict, day: str, window: dict):
    """Make window the only window of a day type"""
    schedule[day] = [window]
    allow(schedule, day)


def add_window(schedule: dict, day: str, window: dict) -> bool:
    """Add a window to a day type, keeping windows sorted; False if already present"""
    if window in schedule[day]:
        return False
    schedule[day].append(window)
    schedule[day].sort(key=lambda w: w["start_hour"])
    allow(schedule, day)
    return True


def remove_window(schedule: dict, day: str, window: dict) -> bool:
    """Remove a window from a day type; False if it was not there.

    Removing the last window denies the day type: taking time away must
    never leave the day with no restriction at all.
    """
    if window not in schedule[day]:
        return False
    schedule[day].remove(window)
    if not schedule[day]:
        deny(schedule, day)
    return True


def clear(schedule: dict, day: str):
    """Make a day type unrestricted"""
    schedule[day] = []
    allow(schedule, day)


def is_empty(schedule: dict) -> bool:
    """Whether a schedule restricts nothing, which is what having none means"""
    return all(is_unrestricted(schedule, day) for day in DAY_TYPES)


def format_hour_range(start_hour: int, end_hour: int) -> str:
    if start_hour > end_hour:
        return f"{start_hour:02d}:00 - {end_hour:02d}:00 (overnight)"
    return f"{start_hour:02d}:00 - {end_hour:02d}:00"


def format_windows(windows: list) -> str:
    if not windows:
        return "Unrestricted"
    return ", ".join(format_hour_range(w["start_hour"], w["end_hour"]) for w in windows)


def format_day(schedule: dict, day: str) -> str:
    if is_denied(schedule, day):
        return "No access"
    return format_windows(schedule[day])


def describe(schedule: dict, indent: str = "") -> list:
    """Return the lines that show a normalized schedule to a user"""
    weekday, weekend = schedule["weekday"], schedule["weekend"]
    if weekday == weekend and is_denied(schedule, "weekday") == is_denied(schedule, "weekend"):
        if len(weekday) <= 1:
            return [f"{indent}Access Hours: {format_day(schedule, 'weekday')} (all days)"]
        return [f"{indent}Access Hours (all days):"] + [
            f"{indent}  {format_hour_range(w['start_hour'], w['end_hour'])}" for w in weekday]

    lines = [f"{indent}Access Hours:"]
    for day in DAY_TYPES:
        if len(schedule[day]) <= 1:
            lines.append(f"{indent}  {DAY_LABELS[day]}: {format_day(schedule, day)}")
        else:
            lines.append(f"{indent}  {DAY_LABELS[day]}:")
            lines += [f"{indent}    {format_hour_range(w['start_hour'], w['end_hour'])}"
                      for w in schedule[day]]
    return lines
