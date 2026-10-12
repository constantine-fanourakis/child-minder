"""
Child Minder Management Utility (cmctl)
Provides easy management and reporting for the Child Minder system
"""

import copy
import json
import sys
import argparse
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
import os
import subprocess
import pwd

from childminder import accounts
from childminder import config as cfg
from childminder import schedule
from childminder import store
from childminder.store import atomic_write_json

CONFIG_PATH = "/etc/child-minder/config.json"
STATE_PATH = "/var/lib/child-minder/state.json"
LOG_PATH = "/var/log/child-minder/minder.log"
USER_CONTROL_PATH = "/var/lib/child-minder/user_control.json"

SERVICE = "child-minder"

# Commands that only read state and so do not need root
READ_ONLY_COMMANDS = {"config", "usage", "status", "logs", "groups", "user-status"}


def run_command(argv: list) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


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
        
    def load_config(self) -> dict:
        """Load the configuration file as written, for editing"""
        try:
            return cfg.read_file(self.config_path)
        except cfg.ConfigError as e:
            hint = " (try: sudo cmctl ...)" if os.geteuid() != 0 else ""
            print(f"Error: {e}{hint}")
            sys.exit(1)

    def effective_config(self) -> dict:
        """Load the configuration as the daemon applies it, reporting invalid entries"""
        uid_min, _ = cfg.read_uid_range()
        return cfg.validate(self.load_config(), lambda m: print(f"Warning: {m}"), uid_min)

    @staticmethod
    def _check_minutes(minutes: int) -> bool:
        if minutes < 0:
            print("Error: Minutes must be zero or more (0 allows no time)")
            return False
        return True
            
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
        """Load current state"""
        try:
            with open(self.state_path, 'r') as f:
                state = json.load(f)
                # Ensure group_usage exists for backward compatibility
                if 'group_usage' not in state:
                    state['group_usage'] = {}
                if 'user_daily_usage' not in state:
                    state['user_daily_usage'] = {}
                return state
        except FileNotFoundError:
            pass
        except PermissionError:
            print(f"Error: Permission denied reading {self.state_path} (try: sudo cmctl ...)")
            sys.exit(1)
        except Exception as e:
            print(f"Warning: Could not load state: {e}")
        return {"daily_usage": {}, "group_usage": {}, "user_daily_usage": {},
                "last_reset": datetime.now().isoformat()}
        
    def add_blocked_process(self, process_name: str):
        """Add a process to the block list"""
        config = self.load_config()
        if 'blocked_processes' not in config:
            config['blocked_processes'] = []
        if process_name not in config['blocked_processes']:
            config['blocked_processes'].append(process_name)
            self.save_config(config)
            print(f"Added '{process_name}' to blocked processes")
        else:
            print(f"'{process_name}' is already blocked")
            
    def remove_blocked_process(self, process_name: str):
        """Remove a process from the block list"""
        config = self.load_config()
        if process_name in config.get('blocked_processes', []):
            config['blocked_processes'].remove(process_name)
            self.save_config(config)
            print(f"Removed '{process_name}' from blocked processes")
        else:
            print(f"'{process_name}' is not in blocked processes")
            
    def add_to_group(self, group_name: str, process_name: str):
        """Add a process to a group"""
        config = self.load_config()
        if 'process_groups' not in config:
            config['process_groups'] = {}
        if group_name not in config['process_groups']:
            config['process_groups'][group_name] = []
        if process_name not in config['process_groups'][group_name]:
            config['process_groups'][group_name].append(process_name)
            self.save_config(config)
            print(f"Added '{process_name}' to group '{group_name}'")
        else:
            print(f"'{process_name}' is already in group '{group_name}'")
    
    def remove_from_group(self, group_name: str, process_name: str):
        """Remove a process from a group"""
        config = self.load_config()
        if 'process_groups' in config and group_name in config['process_groups']:
            if process_name in config['process_groups'][group_name]:
                config['process_groups'][group_name].remove(process_name)
                if not config['process_groups'][group_name]:
                    del config['process_groups'][group_name]
                self.save_config(config)
                print(f"Removed '{process_name}' from group '{group_name}'")
            else:
                print(f"'{process_name}' is not in group '{group_name}'")
        else:
            print(f"Group '{group_name}' does not exist")
    
    def set_group_limit(self, group_name: str, minutes: int):
        """Set time limit for a group"""
        if not self._check_minutes(minutes):
            return
        config = self.load_config()
        if 'group_limits' not in config:
            config['group_limits'] = {}
        config['group_limits'][group_name] = minutes
        self.save_config(config)
        print(f"Set time limit for group '{group_name}' to {minutes} minutes")
    
    def remove_group_limit(self, group_name: str):
        """Remove time limit for a group"""
        config = self.load_config()
        if 'group_limits' in config and group_name in config['group_limits']:
            del config['group_limits'][group_name]
            self.save_config(config)
            print(f"Removed time limit for group '{group_name}'")
        else:
            print(f"Group '{group_name}' has no time limit set")

    def set_user_daily_limit(self, username: str, minutes: int, day_type: str = 'both'):
        """Set overall daily screen time limit for a user (weekday, weekend, or both)"""
        if not self._check_minutes(minutes):
            return
        config = self.load_config()
        config.setdefault('user_daily_limits', {})
        entry = config['user_daily_limits'].get(username, {})
        if isinstance(entry, (int, float)):
            entry = {"weekday": int(entry), "weekend": int(entry)}
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

    def remove_user_daily_limit(self, username: str):
        """Remove overall daily screen time limit for a user"""
        config = self.load_config()
        if username in config.get('user_daily_limits', {}):
            del config['user_daily_limits'][username]
            self.save_config(config)
            print(f"Removed daily screen time limit for '{username}'")
        else:
            print(f"'{username}' has no daily screen time limit set")
    
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
    
    def set_time_limit(self, process_name: str, minutes: int):
        """Set time limit for a process"""
        if not self._check_minutes(minutes):
            return
        config = self.load_config()
        if 'limited_processes' not in config:
            config['limited_processes'] = {}
        config['limited_processes'][process_name] = minutes
        self.save_config(config)
        print(f"Set time limit for '{process_name}' to {minutes} minutes")

    def remove_time_limit(self, process_name: str):
        """Remove time limit for a process"""
        config = self.load_config()
        if process_name in config.get('limited_processes', {}):
            del config['limited_processes'][process_name]
            self.save_config(config)
            print(f"Removed time limit for '{process_name}'")
        else:
            print(f"'{process_name}' has no time limit set")

    def add_monitored_user(self, username: str):
        """Add a user to monitor"""
        config = self.load_config()
        if 'monitored_users' not in config:
            config['monitored_users'] = []
        if username not in config['monitored_users']:
            config['monitored_users'].append(username)
            self.save_config(config)
            print(f"Added '{username}' to monitored users")
        else:
            print(f"'{username}' is already monitored")
            
    def remove_monitored_user(self, username: str):
        """Remove a user from monitoring"""
        config = self.load_config()
        if username in config.get('monitored_users', []):
            config['monitored_users'].remove(username)
            self.save_config(config)
            print(f"Removed '{username}' from monitored users")
        else:
            print(f"'{username}' is not monitored")
            
    def show_config(self):
        """Display current configuration"""
        config = self.effective_config()
        print("\n=== Current Configuration ===")
        print(f"Enabled: {config.get('enabled', True)}")
        print(f"Check Interval: {config.get('check_interval', 5)} seconds")
        print(f"\nMonitored Users: {', '.join(config.get('monitored_users', [])) or 'All users'}")
        
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
            
    def show_usage(self):
        """Display usage statistics"""
        state = self.load_state()
        config = self.effective_config()
        print("\n=== Daily Usage Statistics ===")
        print(f"Last reset: {state.get('last_reset', 'Unknown')}")
        
        # Show individual process usage
        daily_usage = state.get('daily_usage', {})
        if daily_usage:
            print("\nIndividual Process Usage:")
            for user, processes in daily_usage.items():
                print(f"\nUser: {user}")
                for process, seconds in processes.items():
                    minutes = seconds / 60
                    hours = minutes / 60
                    if hours >= 1:
                        print(f"  - {process}: {hours:.1f} hours")
                    else:
                        print(f"  - {process}: {minutes:.0f} minutes")
                    
                    # Check if limited
                    limit = config['limited_processes'].get(process)
                    if limit is not None:
                        remaining = max(0, limit - minutes)
                        print(f"      (Limit: {limit} min, Remaining: {remaining:.0f} min)")
        
        # Show group usage
        group_usage = state.get('group_usage', {})
        if group_usage:
            print("\nGroup Usage:")
            for user, groups in group_usage.items():
                print(f"\nUser: {user}")
                for group, seconds in groups.items():
                    minutes = seconds / 60
                    hours = minutes / 60
                    if hours >= 1:
                        print(f"  - Group '{group}': {hours:.1f} hours")
                    else:
                        print(f"  - Group '{group}': {minutes:.0f} minutes")
                    
                    # Check if limited
                    limit = config['group_limits'].get(group)
                    if limit is not None:
                        remaining = max(0, limit - minutes)
                        print(f"      (Limit: {limit} min, Remaining: {remaining:.0f} min)")
        
        # Show user daily screen time
        user_daily_usage = state.get('user_daily_usage', {})
        if user_daily_usage:
            print("\nUser Daily Screen Time:")
            now = datetime.now()
            for user, seconds in user_daily_usage.items():
                minutes = seconds / 60
                if minutes >= 60:
                    print(f"  {user}: {minutes/60:.1f} hours")
                else:
                    print(f"  {user}: {minutes:.0f} minutes")
                limit = cfg.user_daily_limit(config, user, now)
                if limit is not None:
                    remaining = max(0, limit - minutes)
                    print(f"    ({cfg.day_type(now)} limit: {limit} min, "
                          f"Remaining: {remaining:.0f} min)")

        if not daily_usage and not group_usage and not user_daily_usage:
            print("No usage data available")
                    
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
                return
            print("Usage statistics reset requested; the service will clear its counters")
            return

        state = self.load_state()
        state['daily_usage'] = {}
        state['group_usage'] = {}
        state['user_daily_usage'] = {}
        state['last_reset'] = datetime.now().isoformat()
        try:
            atomic_write_json(self.state_path, state)
            print("Usage statistics reset successfully")
        except Exception as e:
            print(f"Error resetting usage: {e}")
            
    def service_status(self):
        """Show service status"""
        result = subprocess.run(['systemctl', 'status', 'child-minder'],
                              capture_output=True, text=True)
        print(result.stdout)
        
    def view_logs(self, lines: int = 50):
        """View recent log entries"""
        print(f"\n=== Last {lines} log entries ===")
        result = subprocess.run(['tail', '-n', str(lines), str(self.log_path)], 
                              capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error: {result.stderr.strip()} (try: sudo cmctl logs)")
            return
        print(result.stdout)
        
    def enable_monitoring(self, enable: bool = True):
        """Enable or disable monitoring"""
        config = self.load_config()
        config['enabled'] = enable
        self.save_config(config)
        status = "enabled" if enable else "disabled"
        print(f"Monitoring {status}")
    
    def disable_user_account(self, username: str, reason: str = "Administrative action",
                            hours: Optional[float] = None):
        """Disable a user account"""
        if hours is not None and not 0 < hours < float("inf"):
            print("Error: Duration must be a positive number of hours")
            return
        try:
            try:
                uid = pwd.getpwnam(username).pw_uid
            except KeyError:
                print(f"Error: User {username} does not exist")
                return
            # The daemon never acts on system accounts, and expiring one such
            # as root can lock the administrator out.
            _, uid_max = cfg.read_uid_range()
            if not self.effective_config()["min_uid"] <= uid <= uid_max:
                print(f"Error: {username} is a system account and cannot be disabled")
                return

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
                    return

            if hours is not None:
                print(f"User {username} disabled for {hours} hours")
            else:
                print(f"User {username} disabled until manually re-enabled")

            subprocess.run(['loginctl', 'terminate-user', username], check=False,
                           capture_output=True)
            subprocess.run(['pkill', '-u', username], check=False, capture_output=True)
            
            print(f"Account disabled and sessions terminated for: {username}")
            print(f"Reason: {reason}")
            
        except Exception as e:
            print(f"Error disabling user: {e}")
    
    def enable_user_account(self, username: str):
        """Enable a user account"""
        try:
            with self._edit_user_control() as user_control:
                error, note = accounts.unlock(username, run_command)
                if error:
                    print(f"Error: Failed to enable account: {error}")
                    return
                if note:
                    print(f"Note: password stayed locked ({note})")

                user_control["disabled_users"].pop(username, None)

            print(f"User account enabled: {username}")
            
        except Exception as e:
            print(f"Error enabling user: {e}")
    
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
            return self.user_control.load(lambda m: print(f"Warning: {m}"))
        except store.StoreError as e:
            hint = " (try: sudo cmctl ...)" if os.geteuid() != 0 else ""
            print(f"Error: {e}{hint}")
            sys.exit(1)

    def _save_user_control(self, user_control: dict):
        """Save account disables and schedules; the caller holds the lock"""
        self.user_control.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.user_control.path, user_control)
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
        """Yield account disables and schedules under the lock, saving any change"""
        with self.user_control.locked():
            user_control = self._load_user_control()
            before = copy.deepcopy(user_control)
            yield user_control
            if user_control != before:
                self._save_user_control(user_control)

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
            return
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

    def add_window(self, username: str, day: str, start_hour: int, end_hour: int):
        """Add an access window to a day type"""
        window = self._window(start_hour, end_hour)
        if window is None:
            return
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            if not schedule.add_window(sched, day, window):
                print(f"Window {schedule.format_hour_range(start_hour, end_hour)} "
                      f"already exists for {day}s")
                return
            self._store_schedule(user_control, username, sched)
            print(f"Added {day} window for {username}: "
                  f"{schedule.format_hour_range(start_hour, end_hour)}")
            print(f"{day.capitalize()} windows: {schedule.format_windows(sched[day])}")

    def remove_window(self, username: str, day: str, start_hour: int, end_hour: int):
        """Remove an access window from a day type"""
        with self._edit_user_control() as user_control:
            if username not in user_control["daily_schedules"]:
                print(f"No schedule found for user {username}")
                return
            sched = self._user_schedule(user_control, username)
            window = {"start_hour": start_hour, "end_hour": end_hour}
            if not schedule.remove_window(sched, day, window):
                print(f"Window {schedule.format_hour_range(start_hour, end_hour)} "
                      f"not found in {day} schedule")
                return
            self._store_schedule(user_control, username, sched)
            print(f"Removed {day} window for {username}: "
                  f"{schedule.format_hour_range(start_hour, end_hour)}")
            if sched[day]:
                print(f"Remaining {day} windows: {schedule.format_windows(sched[day])}")
            else:
                print(f"No {day} windows remaining - {day} access is now unrestricted")

    def clear_windows(self, username: str, day: str):
        """Make a day type unrestricted"""
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            label = day.capitalize()
            if schedule.is_unrestricted(sched, day):
                print(f"{label} access for {username} is already unrestricted")
                return
            schedule.clear(sched, day)
            self._store_schedule(user_control, username, sched)
            if schedule.is_empty(sched):
                print(f"Cleared {day} windows for {username}; "
                      f"access is now unrestricted at any time")
            else:
                other = self._other_day(day)
                print(f"Cleared {day} windows for {username}; {day} access is now unrestricted")
                print(f"{other.capitalize()} access: {schedule.format_day(sched, other)}")

    def deny_access(self, username: str, day: str):
        """Deny all access on a day type, replacing any windows it had"""
        with self._edit_user_control() as user_control:
            sched = self._user_schedule(user_control, username)
            if schedule.is_denied(sched, day):
                print(f"{username} already has no {day} access")
                return
            schedule.deny(sched, day)
            self._store_schedule(user_control, username, sched)
            days = "Mon-Fri" if day == "weekday" else "Sat-Sun"
            print(f"{username} now has no {day} access ({days}) and will be logged out")
            print(f"Restore it with: cmctl set-{day}-hours {username} START END, "
                  f"or cmctl clear-{day}-windows {username} for unrestricted access")

    def clear_user_hours(self, username: str):
        """Remove a user's access schedule entirely, allowing access at any time"""
        with self._edit_user_control() as user_control:
            if username not in user_control["daily_schedules"]:
                print(f"Access hours for {username} are already unrestricted")
                return
            del user_control["daily_schedules"][username]
            print(f"Cleared all access hours for {username}; "
                  f"access is now unrestricted at any time")

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
                    return
                
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
                        
        except Exception as e:
            print(f"Error showing user status: {e}")

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
        description='Disable the child-minder monitoring service entirely. While disabled, no time tracking or process termination will occur. Use "cmctl enable" to resume monitoring.',
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
        manager.add_blocked_process(args.process)
    elif args.command == 'unblock':
        manager.remove_blocked_process(args.process)
    elif args.command == 'limit':
        manager.set_time_limit(args.process, args.minutes)
    elif args.command == 'unlimit':
        manager.remove_time_limit(args.process)
    elif args.command == 'add-to-group':
        manager.add_to_group(args.group, args.process)
    elif args.command == 'remove-from-group':
        manager.remove_from_group(args.group, args.process)
    elif args.command == 'group-limit':
        manager.set_group_limit(args.group, args.minutes)
    elif args.command == 'group-unlimit':
        manager.remove_group_limit(args.group)
    elif args.command == 'user-limit':
        manager.set_user_daily_limit(args.username, args.minutes, 'both')
    elif args.command == 'user-weekday-limit':
        manager.set_user_daily_limit(args.username, args.minutes, 'weekday')
    elif args.command == 'user-weekend-limit':
        manager.set_user_daily_limit(args.username, args.minutes, 'weekend')
    elif args.command == 'user-unlimit':
        manager.remove_user_daily_limit(args.username)
    elif args.command == 'groups':
        manager.list_groups()
    elif args.command == 'add-user':
        manager.add_monitored_user(args.username)
    elif args.command == 'remove-user':
        manager.remove_monitored_user(args.username)
    elif args.command == 'config':
        manager.show_config()
    elif args.command == 'usage':
        manager.show_usage()
    elif args.command == 'status':
        manager.service_status()
    elif args.command == 'reset':
        manager.reset_usage()
    elif args.command == 'logs':
        manager.view_logs(args.lines)
    elif args.command == 'enable':
        manager.enable_monitoring(True)
    elif args.command == 'disable':
        manager.enable_monitoring(False)
    elif args.command == 'disable-user':
        manager.disable_user_account(args.username, args.reason, args.hours)
    elif args.command == 'enable-user':
        manager.enable_user_account(args.username)
    elif args.command in ('set-weekday-hours', 'set-weekend-hours'):
        manager.set_hours(args.username, args.command.split('-')[1], args.start, args.end)
    elif args.command == 'set-user-hours':
        manager.set_hours(args.username, "weekday", args.start, args.end,
                          fill_empty_weekend=True)
    elif args.command in ('add-weekday-window', 'add-weekend-window'):
        manager.add_window(args.username, args.command.split('-')[1], args.start, args.end)
    elif args.command in ('remove-weekday-window', 'remove-weekend-window'):
        manager.remove_window(args.username, args.command.split('-')[1], args.start, args.end)
    elif args.command in ('clear-weekday-windows', 'clear-weekend-windows'):
        manager.clear_windows(args.username, args.command.split('-')[1])
    elif args.command in ('deny-weekday-access', 'deny-weekend-access'):
        manager.deny_access(args.username, args.command.split('-')[1])
    elif args.command == 'clear-user-hours':
        manager.clear_user_hours(args.username)
    elif args.command == 'user-status':
        manager.show_user_status(args.username)
