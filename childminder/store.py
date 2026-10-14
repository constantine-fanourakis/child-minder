"""On-disk state shared by the daemon and cmctl.

File ownership:
- config.json: written by cmctl under its config.json.lock, read by the daemon
- state.json: written by the daemon; cmctl rewrites it only while the
  service is stopped
- user_control.json: written by both, always inside UserControlStore.update(),
  which holds an exclusive lock on the file beside it for the whole
  read-modify-write, including any account change it is recording

Every write is atomic, through a temp file unique to the writer.
"""

import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

from childminder import schedule

USER_CONTROL_NAME = "user_control.json"

Warn = Callable[[str], None]


class StoreError(Exception):
    """Raised when a stored file exists but cannot be read or parsed"""


def atomic_write_json(path: Path, data: dict):
    """Write JSON to path atomically, so a reader never sees a partial file.

    The temp file is unique, so concurrent writers never share one, and
    mkstemp creates it 0600: the rename replaces the original file, mode
    included, and none of these files are for the monitored user.
    """
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Optional[dict]:
    """Return the JSON object in path, None if it does not exist, or raise StoreError"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except PermissionError:
        raise StoreError(f"Permission denied reading {path}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise StoreError(f"Invalid JSON in {path}: {e}")
    except OSError as e:
        raise StoreError(f"Could not read {path}: {e}")
    if not isinstance(data, dict):
        raise StoreError(f"{path} must contain a JSON object")
    return data


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on a stable sidecar file, not the replaced JSON file"""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def normalize_user_control(raw: dict, warn: Optional[Warn] = None) -> dict:
    """Return user control data with both sections present and every schedule current.

    A disable record that is not an object is kept as a permanent disable,
    and an unreadable re_enable_at is dropped, also leaving it permanent:
    an unreadable record never lets an account back in early.
    """
    disabled = raw.get("disabled_users")
    if not isinstance(disabled, dict):
        disabled = {}
    records = {}
    for username, record in disabled.items():
        if not isinstance(record, dict):
            record = {}
        record = dict(record)
        if "re_enable_at" in record and parse_time(record["re_enable_at"]) is None:
            if warn:
                warn(f"Ignoring unreadable re_enable_at for {username}; "
                     f"the account stays disabled until re-enabled")
            del record["re_enable_at"]
        records[username] = record

    schedules = raw.get("daily_schedules")
    if not isinstance(schedules, dict):
        schedules = {}
    data = dict(raw)
    data["disabled_users"] = records
    data["daily_schedules"] = {
        username: schedule.normalize(sched, warn) for username, sched in schedules.items()}
    return data


def parse_time(value) -> Optional[datetime]:
    """Parse an ISO timestamp as naive local time, which is what cmctl writes.

    A timestamp with an offset is converted, so it compares with
    datetime.now() instead of raising TypeError.
    """
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def disable_expired(record: dict, now: datetime) -> bool:
    """Whether a disable record is a timed disable whose time has come"""
    re_enable_at = parse_time(record.get("re_enable_at"))
    return re_enable_at is not None and now >= re_enable_at


class UserControlStore:
    """user_control.json: account disables and access schedules"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def load(self, warn: Optional[Warn] = None) -> dict:
        """Read the file, which may be missing, raising StoreError if it is unusable"""
        raw = read_json(self.path)
        return normalize_user_control(raw or {}, warn)

    @contextmanager
    def locked(self) -> Iterator[None]:
        with file_lock(self.lock_path):
            yield

    def save(self, data: dict):
        """Write the file; the caller holds the lock"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, data)

    @contextmanager
    def update(self, warn: Optional[Warn] = None,
               on_save: Optional[Callable[[], None]] = None) -> Iterator[dict]:
        """Yield the current data under the lock, saving it afterwards if it changed.

        on_save runs after a save, still under the lock. Nothing is saved if
        the body raises.
        """
        with self.locked():
            data = self.load(warn)
            before = copy.deepcopy(data)
            yield data
            if data != before:
                self.save(data)
                if on_save:
                    on_save()
