"""
Child Minder Management Utility (cmctl)
Provides easy management and reporting for the Child Minder system
"""

import sys
import argparse
import getpass
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
import os
import subprocess
import pwd

from childminder import accounts
from childminder import config as cfg
from childminder import policy
from childminder import schedule
from childminder import store
from childminder import webauth
from childminder.store import atomic_write_json

CONFIG_PATH = "/etc/child-minder/config.json"
STATE_PATH = "/var/lib/child-minder/state.json"
LOG_PATH = "/var/log/child-minder/minder.log"
USER_CONTROL_PATH = "/var/lib/child-minder/user_control.json"
WEB_CONFIG_PATH = "/etc/child-minder/web.json"

SERVICE = "child-minder"

# Commands that only read state and so do not need root
READ_ONLY_COMMANDS = {"config", "usage", "status", "logs", "groups", "user-status"}


def run_command(argv: list):
    """Run argv, returning None if it could not be run (the Runner contract)"""
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        return None


def _print_warning(message: str):
    print(f"Warning: {message}")


def _exit_unreadable(error: Exception):
    """Report a file cmctl cannot use and exit, hinting at sudo when not root"""
    hint = " (try: sudo cmctl ...)" if os.geteuid() != 0 else ""
    print(f"Error: {error}{hint}")
    sys.exit(1)


def service_is_active() -> bool:
    """Check whether the child-minder service is currently running"""
    result = subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE], check=False)
    return result.returncode == 0


class ChildMinderManager:
    def __init__(self):
        self.config_path = Path(CONFIG_PATH)
        self.state_path = Path(STATE_PATH)
        self.log_path = Path(LOG_PATH)
        self.user_control = store.UserControlStore(Path(USER_CONTROL_PATH))
        self.web_config_path = Path(WEB_CONFIG_PATH)
        
    def load_config(self) -> dict:
        """Load the configuration file as written, for editing"""
        try:
            return cfg.read_file(self.config_path)
        except cfg.ConfigError as e:
            _exit_unreadable(e)

    def effective_config(self) -> dict:
        """Load the configuration as the daemon applies it, reporting invalid entries"""
        uid_min, _ = cfg.read_uid_range()
        return cfg.validate(self.load_config(), _print_warning, uid_min)

    @staticmethod
    def _check_minutes(minutes: int) -> bool:
        if minutes < 0:
            print("Error: Minutes must be zero or more (0 allows no time)")
            return False
        return True

    @staticmethod
    def _list_entry(config: dict, key: str) -> list:
        """Return config[key] as a list, replacing a wrong-typed value.

        Mutating commands edit the config as written, so the entry they are
        about to touch cannot be trusted to have its documented type.
        """
        value = config.get(key)
        if not isinstance(value, list):
            if key in config:
                _print_warning(f"Ignoring invalid '{key}'; starting it empty")
            config[key] = value = []
        return value

    @staticmethod
    def _dict_entry(config: dict, key: str) -> dict:
        """Return config[key] as a dict, replacing a wrong-typed value"""
        value = config.get(key)
        if not isinstance(value, dict):
            if key in config:
                _print_warning(f"Ignoring invalid '{key}'; starting it empty")
            config[key] = value = {}
        return value

    def _regular_account_uid(self, username: str) -> Optional[int]:
        """Return username's UID if it is a regular account, else report why not"""
        try:
            uid = pwd.getpwnam(username).pw_uid
        except KeyError:
            print(f"Error: User {username} does not exist")
            return None
        _, uid_max = cfg.read_uid_range()
        if not self.effective_config()["min_uid"] <= uid <= uid_max:
            print(f"Error: {username} is a system account and cannot be managed")
            return None
        return uid

    def save_config(self, config: dict):
        """Save configuration and ask the running service to pick it up"""
        try:
            atomic_write_json(self.config_path, config)
            print("Configuration saved successfully")
            subprocess.run(['systemctl', 'reload', SERVICE], check=False)
        except Exception as e:
            print(f"Error saving config: {e}")
            sys.exit(1)
            
    def load_state(self) -> dict:
        """Load the daemon's saved state, empty if there is none.

        As root an unreadable file is reported and treated as empty, as the
        daemon does; anyone else is most likely just not allowed to read it.
        """
        try:
            state = store.read_json(self.state_path)
        except store.StoreError as e:
            if os.geteuid() != 0:
                _exit_unreadable(e)
            print(f"Warning: Could not load state: {e}")
            state = None
        return state or {"last_reset": datetime.now().isoformat()}
        
    def add_blocked_process(self, process_name: str):
        """Add a process to the block list"""
        config = self.load_config()
        blocked = self._list_entry(config, 'blocked_processes')
        if process_name not in blocked:
            blocked.append(process_name)
            self.save_config(config)
            print(f"Added '{process_name}' to blocked processes")
        else:
            print(f"'{process_name}' is already blocked")
        return True

    def remove_blocked_process(self, process_name: str):
        """Remove a process from the block list"""
        config = self.load_config()
        blocked = self._list_entry(config, 'blocked_processes')
        if process_name in blocked:
            blocked.remove(process_name)
            self.save_config(config)
            print(f"Removed '{process_name}' from blocked processes")
            return True
        print(f"'{process_name}' is not in blocked processes")
        return False

    def add_to_group(self, group_name: str, process_name: str):
        """Add a process to a group"""
        config = self.load_config()
        groups = self._dict_entry(config, 'process_groups')
        if not isinstance(groups.get(group_name), list):
            if group_name in groups:
                _print_warning(f"Ignoring invalid process group '{group_name}';"
                               " starting it empty")
            groups[group_name] = []
        if process_name not in groups[group_name]:
            groups[group_name].append(process_name)
            self.save_config(config)
            print(f"Added '{process_name}' to group '{group_name}'")
        else:
            print(f"'{process_name}' is already in group '{group_name}'")
        return True

    def remove_from_group(self, group_name: str, process_name: str):
        """Remove a process from a group"""
        config = self.load_config()
        groups = self._dict_entry(config, 'process_groups')
        members = groups.get(group_name)
        if isinstance(members, list) and process_name in members:
            members.remove(process_name)
            if not members:
                del groups[group_name]
            self.save_config(config)
            print(f"Removed '{process_name}' from group '{group_name}'")
            return True
        if isinstance(members, list):
            print(f"'{process_name}' is not in group '{group_name}'")
        else:
            print(f"Group '{group_name}' does not exist")
        return False

    def set_group_limit(self, group_name: str, minutes: int):
        """Set time limit for a group"""
        if not self._check_minutes(minutes):
            return False
        config = self.load_config()
        self._dict_entry(config, 'group_limits')[group_name] = minutes
        self.save_config(config)
        print(f"Set time limit for group '{group_name}' to {minutes} minutes")
        return True

    def remove_group_limit(self, group_name: str):
        """Remove time limit for a group"""
        config = self.load_config()
        limits = self._dict_entry(config, 'group_limits')
        if group_name in limits:
            del limits[group_name]
            self.save_config(config)
            print(f"Removed time limit for group '{group_name}'")
            return True
        print(f"Group '{group_name}' has no time limit set")
        return False

    def set_user_daily_limit(self, username: str, minutes: int, day_type: str = 'both'):
        """Set overall daily screen time limit for a user (weekday, weekend, or both)"""
        if not self._check_minutes(minutes):
            return False
        config = self.load_config()
        limits = self._dict_entry(config, 'user_daily_limits')
        entry = limits.get(username, {})
        if cfg.is_minutes(entry):
            entry = {"weekday": entry, "weekend": entry}
        elif isinstance(entry, dict):
            # A wrong-typed sibling day would make the daemon drop the whole
            # entry, so drop only that day before setting the other one.
            kept = {day: entry[day] for day in cfg.DAY_TYPES
                    if cfg.is_minutes(entry.get(day))}
            if len(kept) != sum(day in entry for day in cfg.DAY_TYPES):
                _print_warning(f"Ignoring invalid daily limit values for '{username}'")
            entry = kept
        else:
            if username in limits:
                _print_warning(f"Ignoring invalid daily limit for '{username}';"
                               " starting it empty")
            entry = {}
        if day_type in ('weekday', 'both'):
            entry['weekday'] = minutes
        if day_type in ('weekend', 'both'):
            entry['weekend'] = minutes
        config['user_daily_limits'][username] = entry
        self.save_config(config)
        if day_type == 'both':
            print(f"Set daily screen time limit for '{username}' to {minutes} minutes (weekday and weekend)")
        else:
            print(f"Set {day_type} daily screen time limit for '{username}' to {minutes} minutes")
        return True

    def remove_user_daily_limit(self, username: str):
        """Remove overall daily screen time limit for a user"""
        config = self.load_config()
        limits = self._dict_entry(config, 'user_daily_limits')
        if username in limits:
            del limits[username]
            self.save_config(config)
            print(f"Removed daily screen time limit for '{username}'")
            return True
        print(f"'{username}' has no daily screen time limit set")
        return False
    
    def list_groups(self):
        """List all process groups and their limits"""
        config = self.effective_config()
        print("\n=== Process Groups ===")
        process_groups = config.get('process_groups', {})
        group_limits = config.get('group_limits', {})
        
        if not process_groups:
            print("No process groups defined")
        else:
            for group_name, processes in process_groups.items():
                limit = group_limits.get(group_name, 'No limit')
                if limit != 'No limit':
                    limit = f"{limit} minutes/day"
                print(f"\n{group_name} (Limit: {limit}):")
                for proc in processes:
                    print(f"  - {proc}")
        return True
    
    def set_time_limit(self, process_name: str, minutes: int):
        """Set time limit for a process"""
        if not self._check_minutes(minutes):
            return False
        config = self.load_config()
        self._dict_entry(config, 'limited_processes')[process_name] = minutes
        self.save_config(config)
        print(f"Set time limit for '{process_name}' to {minutes} minutes")
        return True

    def remove_time_limit(self, process_name: str):
        """Remove time limit for a process"""
        config = self.load_config()
        limits = self._dict_entry(config, 'limited_processes')
        if process_name in limits:
            del limits[process_name]
            self.save_config(config)
            print(f"Removed time limit for '{process_name}'")
            return True
        print(f"'{process_name}' has no time limit set")
        return False

    def add_monitored_user(self, username: str):
        """Add a user to monitor"""
        config = self.load_config()
        users = self._list_entry(config, 'monitored_users')
        if username not in users:
            users.append(username)
            self.save_config(config)
            print(f"Added '{username}' to monitored users")
        else:
            print(f"'{username}' is already monitored")
        return True

    def remove_monitored_user(self, username: str):
        """Remove a user from monitoring"""
        config = self.load_config()
        users = self._list_entry(config, 'monitored_users')
        if username in users:
            users.remove(username)
            self.save_config(config)
            print(f"Removed '{username}' from monitored users")
            return True
        print(f"'{username}' is not monitored")
        return False
            
    def show_config(self):
        """Display current configuration"""
        config = self.effective_config()
        print("\n=== Current Configuration ===")
        print(f"Enabled: {config.get('enabled', True)}")
        print(f"Check Interval: {config.get('check_interval', 5)} seconds")
        users = config.get('monitored_users', [])
        users_text = ("None (invalid monitored_users)" if users is None
                      else ', '.join(users) or 'All users')
        print(f"\nMonitored Users: {users_text}")
        
        print(f"\nBlocked Processes:")
        for proc in config.get('blocked_processes', []):
            print(f"  - {proc}")
        
        print(f"\nProcess Groups:")
        for group_name, processes in config.get('process_groups', {}).items():
            limit = config.get('group_limits', {}).get(group_name)
            limit_str = f" ({limit} min/day)" if limit is not None else ""
            print(f"  {group_name}{limit_str}: {', '.join(processes)}")
        
        print(f"\nIndividual Time Limits:")
        for proc, minutes in config.get('limited_processes', {}).items():
            print(f"  - {proc}: {minutes} minutes/day")

        print(f"\nUser Daily Screen Time Limits:")
        for user, entry in config['user_daily_limits'].items():
            weekday = entry['weekday']
            weekend = entry['weekend']
            if weekday == weekend:
                print(f"  - {user}: {weekday} min/day (weekday and weekend)")
            else:
                parts = []
                if weekday is not None:
                    parts.append(f"weekday: {weekday} min")
                if weekend is not None:
                    parts.append(f"weekend: {weekend} min")
                print(f"  - {user}: {', '.join(parts)}")

        print(f"\nMonitored Processes (for logging):")
        for proc in config.get('monitored_processes', []):
            print(f"  - {proc}")
        return True
            
    def show_usage(self):
        """Display usage statistics"""
        state = self.load_state()
        config = self.effective_config()
        print("\n=== Daily Usage Statistics ===")
        print(f"Last reset: {state.get('last_reset', 'Unknown')}")

        now = datetime.now()
        report = policy.usage_report(policy.Usage.from_state(state, _print_warning), config, now)

        def amount(seconds: float) -> str:
            minutes = seconds / 60
            return f"{minutes / 60:.1f} hours" if minutes >= 60 else f"{minutes:.0f} minutes"

        def limit_text(line: policy.UsageLine) -> str:
            return (f"{line.limit_seconds / 60:g} min, "
                    f"Remaining: {line.remaining_seconds / 60:.0f} min")

        for kind, heading in (("process", "Individual Process Usage:"), ("group", "Group Usage:")):
            if not report[kind]:
                continue
            print(f"\n{heading}")
            user = None
            for line in report[kind]:
                if line.username != user:
                    user = line.username
                    print(f"\nUser: {user}")
                name = line.subject if kind == "process" else f"Group '{line.subject}'"
                print(f"  - {name}: {amount(line.seconds)}")
                if line.limit_seconds is not None:
                    print(f"      (Limit: {limit_text(line)})")

        if report["daily"]:
            print("\nUser Daily Screen Time:")
            for line in report["daily"]:
                print(f"  {line.username}: {amount(line.seconds)}")
                if line.limit_seconds is not None:
                    print(f"    ({cfg.day_type(now)} limit: {limit_text(line)})")

        if not any(report.values()):
            print("No usage data available")
        return True
                    
    def reset_usage(self):
        """Reset usage statistics.

        The running daemon keeps counters in memory and rewrites the state file
        every minute, so while it is up the reset is delegated to it by signal;
        writing the file directly would be undone within the minute.
        """
        if service_is_active():
            # Only the main process: SIGUSR1 terminates the helpers the daemon
            # runs (usermod, passwd), which can leave shadow lock files behind.
            # --kill-who is the spelling systemd releases before 252 accept.
            result = subprocess.run(['systemctl', 'kill', '--kill-who=main',
                                     '--signal=SIGUSR1', SERVICE],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error: Could not signal {SERVICE}: {result.stderr.strip()}")
                return False
            print("Usage statistics reset requested; the service will clear its counters")
            return True

        state = self.load_state()
        state.update(policy.Usage().to_state())
        state['last_reset'] = datetime.now().isoformat()
        try:
            atomic_write_json(self.state_path, state)
            print("Usage statistics reset successfully")
            return True
        except Exception as e:
            print(f"Error resetting usage: {e}")
            return False
            
    def service_status(self):
        """Show service status"""
        result = subprocess.run(['systemctl', 'status', 'child-minder'],
                              capture_output=True, text=True)
        print(result.stdout)
        return True

    def view_logs(self, lines: int = 50):
        """View recent log entries"""
        print(f"\n=== Last {lines} log entries ===")
        result = subprocess.run(['tail', '-n', str(lines), str(self.log_path)], 
                              capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error: {result.stderr.strip()} (try: sudo cmctl logs)")
            return False
        print(result.stdout)
        return True
        
    def enable_monitoring(self, enable: bool = True):
        """Enable or disable monitoring"""
        config = self.load_config()
        config['enabled'] = enable
        self.save_config(config)
        status = "enabled" if enable else "disabled"
        print(f"Monitoring {status}")
        return True
    
    def disable_user_account(self, username: str, reason: str = "Administrative action",
                            hours: Optional[float] = None):
        """Disable a user account"""
        if hours is not None and not 0 < hours < float("inf"):
            print("Error: Duration must be a positive number of hours")
            return False
        try:
            try:
                uid = pwd.getpwnam(username).pw_uid
            except KeyError:
                print(f"Error: User {username} does not exist")
                return False
            # The daemon never acts on system accounts, and expiring one such
            # as root can lock the administrator out.
            _, uid_max = cfg.read_uid_range()
            if not self.effective_config()["min_uid"] <= uid <= uid_max:
                print(f"Error: {username} is a system account and cannot be disabled")
                return False

            disable_info = {
                "disabled_at": datetime.now().isoformat(),
                "reason": reason,
                "disabled_by": os.environ.get("SUDO_USER") or pwd.getpwuid(os.geteuid()).pw_name
            }
            if hours is not None:
                disable_info["re_enable_at"] = (datetime.now() + timedelta(hours=hours)).isoformat()

            # The lock keeps the daemon from re-enabling the account between
            # the record and the expiry. The record is saved first, so a
            # failure to save cannot leave an account locked that the daemon
            # would never re-enable and user-status would not show.
            with self.user_control.locked():
                user_control = self._load_user_control()
                previous = user_control["disabled_users"].get(username)
                user_control["disabled_users"][username] = disable_info
                self._save_user_control(user_control)

                error = accounts.lock(username, run_command)
                if error:
                    print(f"Error: Failed to disable account: {error}")
                    if previous is None:
                        del user_control["disabled_users"][username]
                    else:
                        user_control["disabled_users"][username] = previous
                    self._save_user_control(user_control)
                    return False

            if hours is not None:
                print(f"User {username} disabled for {hours} hours")
            else:
                print(f"User {username} disabled until manually re-enabled")

            subprocess.run(['loginctl', 'terminate-user', username], check=False,
                           capture_output=True)
            subprocess.run(['pkill', '-u', username], check=False, capture_output=True)
            
            print(f"Account disabled and sessions terminated for: {username}")
            print(f"Reason: {reason}")
            return True

        except Exception as e:
            print(f"Error disabling user: {e}")
            return False
    
    def enable_user_account(self, username: str):
        """Enable a user account"""
        if self._regular_account_uid(username) is None:
            return False
        try:
            with self._edit_user_control() as user_control:
                if username not in user_control["disabled_users"]:
                    print(f"Error: {username} is not disabled by Child Minder")
                    return False
                error, note = accounts.unlock(username, run_command)
                if error:
                    print(f"Error: Failed to enable account: {error}")
                    return False
                if note:
                    print(f"Note: password stayed locked ({note})")

                user_control["disabled_users"].pop(username, None)

            print(f"User account enabled: {username}")
            return True

        except Exception as e:
            print(f"Error enabling user: {e}")
            return False
    
    def set_web_password(self):
        """Set the web UI password, read from the terminal or from one line of stdin"""
        if sys.stdin.isatty():
            password = getpass.getpass("New web UI password: ")
            if getpass.getpass("Repeat password: ") != password:
                print("Error: Passwords do not match")
                return False
        else:
            password = sys.stdin.readline().rstrip("\n")
        if not password:
            print("Error: Password must not be empty")
            return False

        try:
            web_config = store.read_json(self.web_config_path) or {}
        except store.StoreError as e:
            _exit_unreadable(e)
        web_config["password"] = webauth.hash_password(password)
        try:
            atomic_write_json(self.web_config_path, web_config)
        except OSError as e:
            print(f"Error saving {self.web_config_path}: {e}")
            return False
        print("Web UI password set")
        return True

    def _window(self, start_hour: int, end_hour: int) -> Optional[dict]:
        """Return the window for an hour range, or None after reporting why it is invalid"""
        error = schedule.hours_error(start_hour, end_hour)
        if error:
            print(f"Error: {error}")
            return None
        return {"start_hour": start_hour, "end_hour": end_hour}

    def _load_user_control(self) -> dict:
        """Load account disables and schedules, exiting if the file is unusable"""
        try:
            return self.user_control.load(_print_warning)
        except store.StoreError as e:
            _exit_unreadable(e)

    def _save_user_control(self, user_control: dict):
        """Save account disables and schedules; the caller holds the lock"""
        self.user_control.save(user_control)
        self._enable_user_control()

    def _enable_user_control(self):
        """Turn on user control in the config, which the daemon needs to act on the file"""
        config = self.load_config()
        control = config.get("user_control")
        if not isinstance(control, dict):
            control = config["user_control"] = {}
        if control.get("enabled") is True:
            return
        control["enabled"] = True
        self.save_config(config)

    @contextmanager
    def _edit_user_control(self):
        """Yield account disables and schedules under the lock, saving any change.

        Only loading the file raises StoreError, so the body is not affected
        by the handler.
        """
        try:
            with self.user_control.update(_print_warning,
                                          on_save=self._enable_user_control) as user_control:
                yield user_control
        except store.StoreError as e:
            _exit_unreadable(e)

    def _user_schedule(self, user_control: dict, username: str) -> dict:
        """Return username's schedule in the current format, empty if there is none"""
        return schedule.normalize(user_control["daily_schedules"].get(username))

    def _store_schedule(self, user_control: dict, username: str, sched: dict):
        """Store username's schedule, dropping it when it no longer restricts anything"""
        if schedule.is_empty(sched):
            user_control["daily_schedules"].pop(username, None)
        else:
            user_control["daily_schedules"][username] = sched

    @staticmethod
    def _other_day(day: str) -> str:
        return "weekend" if day == "weekday" else "weekday"

    def set_hours(self, username: str, day: str, start_hour: int, end_hour: int,
                  fill_empty_weekend: bool = False):
        """Make one window the only allowed access on a day type.

        With fill_empty_weekend, an unrestricted weekend gets the same window.
        """
        window = self._window(start_hour, end_hour)
        if window is None:
            return False
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            schedule.set_window(sched, day, window)
            fill_weekend = fill_empty_weekend and schedule.is_unrestricted(sched, "weekend")
            if fill_weekend:
                schedule.set_window(sched, "weekend", dict(window))
            self._store_schedule(user_control, username, sched)

            days = "Mon-Fri" if day == "weekday" else "Sat-Sun"
            other = self._other_day(day)
            print(f"Set {day} access hours for {username}: "
                  f"{schedule.format_hour_range(start_hour, end_hour)}")
            print(f"User will be automatically logged out outside these hours ({days})")
            if fill_weekend:
                print(f"Weekend (Sat-Sun) hours were unset and now match; change them with: "
                      f"cmctl set-weekend-hours {username} START END")
            elif schedule.is_unrestricted(sched, other):
                print(f"{other.capitalize()} access is unrestricted; set it with: "
                      f"cmctl set-{other}-hours {username} START END")
        return True

    def add_window(self, username: str, day: str, start_hour: int, end_hour: int):
        """Add an access window to a day type"""
        window = self._window(start_hour, end_hour)
        if window is None:
            return False
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            if not schedule.add_window(sched, day, window):
                print(f"Window {schedule.format_hour_range(start_hour, end_hour)} "
                      f"already exists for {day}s")
                return True
            self._store_schedule(user_control, username, sched)
            print(f"Added {day} window for {username}: "
                  f"{schedule.format_hour_range(start_hour, end_hour)}")
            print(f"{day.capitalize()} windows: {schedule.format_windows(sched[day])}")
        return True

    def remove_window(self, username: str, day: str, start_hour: int, end_hour: int):
        """Remove an access window from a day type"""
        with self._edit_user_control() as user_control:
            if username not in user_control["daily_schedules"]:
                print(f"No schedule found for user {username}")
                return False
            sched = self._user_schedule(user_control, username)
            window = {"start_hour": start_hour, "end_hour": end_hour}
            if not schedule.remove_window(sched, day, window):
                print(f"Window {schedule.format_hour_range(start_hour, end_hour)} "
                      f"not found in {day} schedule")
                return False
            self._store_schedule(user_control, username, sched)
            print(f"Removed {day} window for {username}: "
                  f"{schedule.format_hour_range(start_hour, end_hour)}")
            if sched[day]:
                print(f"Remaining {day} windows: {schedule.format_windows(sched[day])}")
            else:
                print(f"No {day} windows remaining - {username} now has no {day} access")
                print(f"Allow it again with: cmctl set-{day}-hours {username} START END, "
                      f"or cmctl clear-{day}-windows {username} for unrestricted access")
        return True

    def clear_windows(self, username: str, day: str):
        """Make a day type unrestricted"""
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            label = day.capitalize()
            if schedule.is_unrestricted(sched, day):
                print(f"{label} access for {username} is already unrestricted")
                return True
            schedule.clear(sched, day)
            self._store_schedule(user_control, username, sched)
            if schedule.is_empty(sched):
                print(f"Cleared {day} windows for {username}; "
                      f"access is now unrestricted at any time")
            else:
                other = self._other_day(day)
                print(f"Cleared {day} windows for {username}; {day} access is now unrestricted")
                print(f"{other.capitalize()} access: {schedule.format_day(sched, other)}")
        return True

    def deny_access(self, username: str, day: str):
        """Deny all access on a day type, replacing any windows it had"""
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            if schedule.is_denied(sched, day):
                print(f"{username} already has no {day} access")
                return True
            schedule.deny(sched, day)
            self._store_schedule(user_control, username, sched)
            days = "Mon-Fri" if day == "weekday" else "Sat-Sun"
            print(f"{username} now has no {day} access ({days}) and will be logged out")
            print(f"Restore it with: cmctl set-{day}-hours {username} START END, "
                  f"or cmctl clear-{day}-windows {username} for unrestricted access")
        return True

    def clear_user_hours(self, username: str):
        """Remove a user's access schedule entirely, allowing access at any time"""
        with self._edit_user_control() as user_control:
            if username not in user_control["daily_schedules"]:
                print(f"Access hours for {username} are already unrestricted")
                return True
            del user_control["daily_schedules"][username]
            print(f"Cleared all access hours for {username}; "
                  f"access is now unrestricted at any time")
        return True

    def show_user_status(self, username: str = None):
        """Show user account status"""
        try:
            user_control = self._load_user_control()

            if username:
                # Show specific user
                print(f"\n=== User Status: {username} ===")
                
                # Check if account exists
                try:
                    pwd.getpwnam(username)
                except KeyError:
                    print(f"User {username} does not exist")
                    return False
                
                # Check if disabled
                if username in user_control.get("disabled_users", {}):
                    info = user_control["disabled_users"][username]
                    print(f"Status: DISABLED")
                    print(f"  Disabled at: {info.get('disabled_at', 'Unknown')}")
                    print(f"  Reason: {info.get('reason', 'Unknown')}")
                    print(f"  Disabled by: {info.get('disabled_by', 'Unknown')}")
                    if 're_enable_at' in info:
                        print(f"  Will re-enable at: {info['re_enable_at']}")
                else:
                    print(f"Status: ENABLED")
                
                # Check access hours
                if username in user_control.get("daily_schedules", {}):
                    for line in schedule.describe(self._user_schedule(user_control, username)):
                        print(line)
                else:
                    print(f"Access Hours: Unrestricted")
            else:
                # Show all disabled users
                disabled = user_control.get("disabled_users", {})
                if disabled:
                    print("\n=== Disabled Users ===")
                    for user, info in disabled.items():
                        print(f"\n{user}:")
                        print(f"  Disabled at: {info.get('disabled_at', 'Unknown')}")
                        print(f"  Reason: {info.get('reason', 'Unknown')}")
                        if 're_enable_at' in info:
                            print(f"  Will re-enable at: {info['re_enable_at']}")
                else:
                    print("No users currently disabled")
                
                # Show users with access hours
                schedules = user_control.get("daily_schedules", {})
                if schedules:
                    print("\n=== Users with Access Hours ===")
                    for user in schedules:
                        print(f"\n{user}:")
                        for line in schedule.describe(self._user_schedule(user_control, user),
                                                      indent="  "):
                            print(line)
            return True

        except Exception as e:
            print(f"Error showing user status: {e}")
            return False

def main():
    parser = argparse.ArgumentParser(description='Child Minder Management Utility (cmctl)')
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Block/unblock commands
    block_parser = subparsers.add_parser('block', help='Add process to block list')
    block_parser.add_argument('process', help='Process name to block')
    
    unblock_parser = subparsers.add_parser('unblock', help='Remove process from block list')
    unblock_parser.add_argument('process', help='Process name to unblock')
    
    # Time limit commands
    limit_parser = subparsers.add_parser('limit', help='Set time limit for process')
    limit_parser.add_argument('process', help='Process name')
    limit_parser.add_argument('minutes', type=int, help='Daily limit in minutes')
    
    unlimit_parser = subparsers.add_parser('unlimit', help='Remove time limit for process')
    unlimit_parser.add_argument('process', help='Process name')
    
    # Group commands
    add_group_parser = subparsers.add_parser('add-to-group', help='Add process to a group')
    add_group_parser.add_argument('group', help='Group name')
    add_group_parser.add_argument('process', help='Process name')
    
    rm_group_parser = subparsers.add_parser('remove-from-group', help='Remove process from a group')
    rm_group_parser.add_argument('group', help='Group name')
    rm_group_parser.add_argument('process', help='Process name')
    
    group_limit_parser = subparsers.add_parser('group-limit', help='Set time limit for a group')
    group_limit_parser.add_argument('group', help='Group name')
    group_limit_parser.add_argument('minutes', type=int, help='Daily limit in minutes')
    
    group_unlimit_parser = subparsers.add_parser('group-unlimit', help='Remove time limit for a group')
    group_unlimit_parser.add_argument('group', help='Group name')

    user_limit_parser = subparsers.add_parser('user-limit', help='Set overall daily screen time limit for a user (weekday and weekend)')
    user_limit_parser.add_argument('username', help='Username')
    user_limit_parser.add_argument('minutes', type=int, help='Daily limit in minutes')

    user_weekday_limit_parser = subparsers.add_parser('user-weekday-limit', help='Set weekday-only daily screen time limit for a user')
    user_weekday_limit_parser.add_argument('username', help='Username')
    user_weekday_limit_parser.add_argument('minutes', type=int, help='Weekday daily limit in minutes')

    user_weekend_limit_parser = subparsers.add_parser('user-weekend-limit', help='Set weekend-only daily screen time limit for a user')
    user_weekend_limit_parser.add_argument('username', help='Username')
    user_weekend_limit_parser.add_argument('minutes', type=int, help='Weekend daily limit in minutes')

    user_unlimit_parser = subparsers.add_parser('user-unlimit', help='Remove overall daily screen time limit for a user')
    user_unlimit_parser.add_argument('username', help='Username')

    subparsers.add_parser('groups', help='List all process groups')
    
    # User management
    adduser_parser = subparsers.add_parser('add-user', help='Add user to monitor')
    adduser_parser.add_argument('username', help='Username to monitor')
    
    rmuser_parser = subparsers.add_parser('remove-user', help='Remove user from monitoring')
    rmuser_parser.add_argument('username', help='Username to stop monitoring')
    
    # Display commands
    subparsers.add_parser('config', help='Show current configuration')
    subparsers.add_parser('usage', help='Show usage statistics')
    subparsers.add_parser('status', help='Show service status')
    subparsers.add_parser('reset', help='Reset usage statistics')
    
    logs_parser = subparsers.add_parser('logs', help='View recent logs')
    logs_parser.add_argument('-n', '--lines', type=int, default=50, help='Number of lines to show')
    
    # Enable/disable
    subparsers.add_parser('enable', help='Enable monitoring',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Enable the child-minder monitoring service. This resumes tracking and enforcing time limits for all monitored users.',
        epilog='Example: sudo cmctl enable')
    subparsers.add_parser('disable', help='Disable monitoring',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Disable the child-minder monitoring service entirely. While disabled, no time tracking, process termination or session ending will occur, though timed account disables still expire on time. Use "cmctl enable" to resume monitoring.',
        epilog='''Example: sudo cmctl disable

Note: To disable a specific user account instead, use "cmctl disable-user <username>"''')
    
    # User account control
    disable_user_parser = subparsers.add_parser('disable-user', help='Disable a user account')
    disable_user_parser.add_argument('username', help='Username to disable')
    disable_user_parser.add_argument('-r', '--reason', default='Administrative action', help='Reason for disabling')
    disable_user_parser.add_argument('-t', '--hours', type=float, help='Duration in hours (permanent if not set)')
    
    enable_user_parser = subparsers.add_parser('enable-user', help='Enable a user account')
    enable_user_parser.add_argument('username', help='Username to enable')
    
    # Weekday hours command
    weekday_hours_parser = subparsers.add_parser('set-weekday-hours', help='Set weekday access hours (Mon-Fri)')
    weekday_hours_parser.add_argument('username', help='Username')
    weekday_hours_parser.add_argument('start', type=int, help='Start hour (0-23)')
    weekday_hours_parser.add_argument('end', type=int, help='End hour (0-23)')

    # Weekend hours command
    weekend_hours_parser = subparsers.add_parser('set-weekend-hours', help='Set weekend access hours (Sat-Sun)')
    weekend_hours_parser.add_argument('username', help='Username')
    weekend_hours_parser.add_argument('start', type=int, help='Start hour (0-23)')
    weekend_hours_parser.add_argument('end', type=int, help='End hour (0-23)')

    # Legacy command: weekday hours, also applied to an unset weekend
    user_hours_parser = subparsers.add_parser(
        'set-user-hours', help='Set weekday access hours, and weekend hours if unset')
    user_hours_parser.add_argument('username', help='Username')
    user_hours_parser.add_argument('start', type=int, help='Start hour (0-23)')
    user_hours_parser.add_argument('end', type=int, help='End hour (0-23)')

    # Add window commands
    add_weekday_window_parser = subparsers.add_parser('add-weekday-window', help='Add weekday access window (Mon-Fri)')
    add_weekday_window_parser.add_argument('username', help='Username')
    add_weekday_window_parser.add_argument('start', type=int, help='Start hour (0-23)')
    add_weekday_window_parser.add_argument('end', type=int, help='End hour (0-23)')

    add_weekend_window_parser = subparsers.add_parser('add-weekend-window', help='Add weekend access window (Sat-Sun)')
    add_weekend_window_parser.add_argument('username', help='Username')
    add_weekend_window_parser.add_argument('start', type=int, help='Start hour (0-23)')
    add_weekend_window_parser.add_argument('end', type=int, help='End hour (0-23)')

    # Remove window commands
    remove_weekday_window_parser = subparsers.add_parser('remove-weekday-window', help='Remove weekday access window')
    remove_weekday_window_parser.add_argument('username', help='Username')
    remove_weekday_window_parser.add_argument('start', type=int, help='Start hour (0-23)')
    remove_weekday_window_parser.add_argument('end', type=int, help='End hour (0-23)')

    remove_weekend_window_parser = subparsers.add_parser('remove-weekend-window', help='Remove weekend access window')
    remove_weekend_window_parser.add_argument('username', help='Username')
    remove_weekend_window_parser.add_argument('start', type=int, help='Start hour (0-23)')
    remove_weekend_window_parser.add_argument('end', type=int, help='End hour (0-23)')

    # Clear window commands
    clear_weekday_parser = subparsers.add_parser('clear-weekday-windows',
        help='Remove all weekday access windows (weekdays become unrestricted)')
    clear_weekday_parser.add_argument('username', help='Username')

    clear_weekend_parser = subparsers.add_parser('clear-weekend-windows',
        help='Remove all weekend access windows (weekends become unrestricted)')
    clear_weekend_parser.add_argument('username', help='Username')

    clear_hours_parser = subparsers.add_parser('clear-user-hours',
        help='Remove a user access schedule entirely (access at any time)')
    clear_hours_parser.add_argument('username', help='Username')

    # Deny commands
    deny_weekday_parser = subparsers.add_parser('deny-weekday-access',
        help='Deny all weekday access (Mon-Fri)')
    deny_weekday_parser.add_argument('username', help='Username')

    deny_weekend_parser = subparsers.add_parser('deny-weekend-access',
        help='Deny all weekend access (Sat-Sun)')
    deny_weekend_parser.add_argument('username', help='Username')

    user_status_parser = subparsers.add_parser('user-status', help='Show user account status')
    user_status_parser.add_argument('username', nargs='?', help='Username (show all if not specified)')

    subparsers.add_parser('web-password', help='Set the web UI password',
        description='Set the password for the web UI. It is prompted for on a terminal, '
                    'otherwise read as one line from standard input.')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command not in READ_ONLY_COMMANDS and os.geteuid() != 0:
        print(f"Error: '{args.command}' changes system state and must be run as root "
              f"(try: sudo cmctl {args.command} ...)")
        sys.exit(1)

    manager = ChildMinderManager()
    
    if args.command == 'block':
        succeeded = manager.add_blocked_process(args.process)
    elif args.command == 'unblock':
        succeeded = manager.remove_blocked_process(args.process)
    elif args.command == 'limit':
        succeeded = manager.set_time_limit(args.process, args.minutes)
    elif args.command == 'unlimit':
        succeeded = manager.remove_time_limit(args.process)
    elif args.command == 'add-to-group':
        succeeded = manager.add_to_group(args.group, args.process)
    elif args.command == 'remove-from-group':
        succeeded = manager.remove_from_group(args.group, args.process)
    elif args.command == 'group-limit':
        succeeded = manager.set_group_limit(args.group, args.minutes)
    elif args.command == 'group-unlimit':
        succeeded = manager.remove_group_limit(args.group)
    elif args.command == 'user-limit':
        succeeded = manager.set_user_daily_limit(args.username, args.minutes, 'both')
    elif args.command == 'user-weekday-limit':
        succeeded = manager.set_user_daily_limit(args.username, args.minutes, 'weekday')
    elif args.command == 'user-weekend-limit':
        succeeded = manager.set_user_daily_limit(args.username, args.minutes, 'weekend')
    elif args.command == 'user-unlimit':
        succeeded = manager.remove_user_daily_limit(args.username)
    elif args.command == 'groups':
        succeeded = manager.list_groups()
    elif args.command == 'add-user':
        succeeded = manager.add_monitored_user(args.username)
    elif args.command == 'remove-user':
        succeeded = manager.remove_monitored_user(args.username)
    elif args.command == 'config':
        succeeded = manager.show_config()
    elif args.command == 'usage':
        succeeded = manager.show_usage()
    elif args.command == 'status':
        succeeded = manager.service_status()
    elif args.command == 'reset':
        succeeded = manager.reset_usage()
    elif args.command == 'logs':
        succeeded = manager.view_logs(args.lines)
    elif args.command == 'enable':
        succeeded = manager.enable_monitoring(True)
    elif args.command == 'disable':
        succeeded = manager.enable_monitoring(False)
    elif args.command == 'disable-user':
        succeeded = manager.disable_user_account(args.username, args.reason, args.hours)
    elif args.command == 'enable-user':
        succeeded = manager.enable_user_account(args.username)
    elif args.command in ('set-weekday-hours', 'set-weekend-hours'):
        succeeded = manager.set_hours(args.username, args.command.split('-')[1], args.start, args.end)
    elif args.command == 'set-user-hours':
        succeeded = manager.set_hours(args.username, "weekday", args.start, args.end,
                          fill_empty_weekend=True)
    elif args.command in ('add-weekday-window', 'add-weekend-window'):
        succeeded = manager.add_window(args.username, args.command.split('-')[1], args.start, args.end)
    elif args.command in ('remove-weekday-window', 'remove-weekend-window'):
        succeeded = manager.remove_window(args.username, args.command.split('-')[1], args.start, args.end)
    elif args.command in ('clear-weekday-windows', 'clear-weekend-windows'):
        succeeded = manager.clear_windows(args.username, args.command.split('-')[1])
    elif args.command in ('deny-weekday-access', 'deny-weekend-access'):
        succeeded = manager.deny_access(args.username, args.command.split('-')[1])
    elif args.command == 'clear-user-hours':
        succeeded = manager.clear_user_hours(args.username)
    elif args.command == 'user-status':
        succeeded = manager.show_user_status(args.username)
    elif args.command == 'web-password':
        succeeded = manager.set_web_password()

    if not succeeded:
        sys.exit(1)
