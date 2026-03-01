#!/usr/bin/env python3
"""
Child Minder Management Utility (cmctl)
Provides easy management and reporting for the Child Minder system
"""

import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
import subprocess
import pwd

CONFIG_PATH = "/etc/child-minder/config.json"
STATE_PATH = "/var/lib/child-minder/state.json"
LOG_PATH = "/var/log/child-minder/minder.log"

class ChildMinderManager:
    def __init__(self):
        self.config_path = Path(CONFIG_PATH)
        self.state_path = Path(STATE_PATH)
        self.log_path = Path(LOG_PATH)
        self.user_control_path = Path("/var/lib/child-minder/user_control.json")
        
    def load_config(self) -> dict:
        """Load current configuration"""
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Error: Config file not found at {self.config_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in config file: {e}")
            sys.exit(1)
            
    def save_config(self, config: dict):
        """Save configuration"""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
            print("Configuration saved successfully")
            # Reload service
            subprocess.run(['systemctl', 'reload', 'child-minder'], check=False)
        except Exception as e:
            print(f"Error saving config: {e}")
            sys.exit(1)
            
    def load_state(self) -> dict:
        """Load current state"""
        try:
            if self.state_path.exists():
                with open(self.state_path, 'r') as f:
                    state = json.load(f)
                    # Ensure group_usage exists for backward compatibility
                    if 'group_usage' not in state:
                        state['group_usage'] = {}
                    if 'user_daily_usage' not in state:
                        state['user_daily_usage'] = {}
                    return state
        except Exception as e:
            print(f"Warning: Could not load state: {e}")
        return {"daily_usage": {}, "group_usage": {}, "last_reset": datetime.now().isoformat()}
        
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
        if minutes <= 0:
            print("Error: Minutes must be a positive number")
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
        if minutes <= 0:
            print("Error: Minutes must be a positive number")
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
        config = self.load_config()
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
        if minutes <= 0:
            print("Error: Minutes must be a positive number")
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
        config = self.load_config()
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
            limit_str = f" ({limit} min/day)" if limit else ""
            print(f"  {group_name}{limit_str}: {', '.join(processes)}")
        
        print(f"\nIndividual Time Limits:")
        for proc, minutes in config.get('limited_processes', {}).items():
            print(f"  - {proc}: {minutes} minutes/day")

        print(f"\nUser Daily Screen Time Limits:")
        for user, entry in config.get('user_daily_limits', {}).items():
            if isinstance(entry, (int, float)):
                print(f"  - {user}: {int(entry)} min/day (weekday and weekend)")
            else:
                weekday = entry.get('weekday')
                weekend = entry.get('weekend')
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
        config = self.load_config()
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
                    limit = config.get('limited_processes', {}).get(process)
                    if limit:
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
                    limit = config.get('group_limits', {}).get(group)
                    if limit:
                        remaining = max(0, limit - minutes)
                        print(f"      (Limit: {limit} min, Remaining: {remaining:.0f} min)")
        
        # Show user daily screen time
        user_daily_usage = state.get('user_daily_usage', {})
        if user_daily_usage:
            print("\nUser Daily Screen Time:")
            is_weekend = datetime.now().weekday() >= 5
            day_type = "weekend" if is_weekend else "weekday"
            for user, seconds in user_daily_usage.items():
                minutes = seconds / 60
                if minutes >= 60:
                    print(f"  {user}: {minutes/60:.1f} hours")
                else:
                    print(f"  {user}: {minutes:.0f} minutes")
                entry = config.get('user_daily_limits', {}).get(user)
                if entry is not None:
                    if isinstance(entry, (int, float)):
                        limit = int(entry)
                    else:
                        limit = entry.get('weekend' if is_weekend else 'weekday')
                    if limit is not None:
                        remaining = max(0, limit - minutes)
                        print(f"    ({day_type} limit: {limit} min, Remaining: {remaining:.0f} min)")

        if not daily_usage and not group_usage and not user_daily_usage:
            print("No usage data available")
                    
    def reset_usage(self):
        """Reset usage statistics"""
        state = self.load_state()
        state['daily_usage'] = {}
        state['group_usage'] = {}
        state['user_daily_usage'] = {}
        state['last_reset'] = datetime.now().isoformat()
        
        try:
            with open(self.state_path, 'w') as f:
                json.dump(state, f, indent=2)
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
        print(result.stdout)
        
    def enable_monitoring(self, enable: bool = True):
        """Enable or disable monitoring"""
        config = self.load_config()
        config['enabled'] = enable
        self.save_config(config)
        status = "enabled" if enable else "disabled"
        print(f"Monitoring {status}")
    
    def disable_user_account(self, username: str, reason: str = "Administrative action",
                            hours: Optional[int] = None):
        """Disable a user account"""
        try:
            # Lock the account
            result = subprocess.run(['sudo', 'passwd', '-l', username], 
                                  capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error: Failed to disable account: {result.stderr}")
                return
            
            # Update user control state
            user_control = {"disabled_users": {}, "scheduled_disables": {}, "daily_schedules": {}}
            if self.user_control_path.exists():
                with open(self.user_control_path, 'r') as f:
                    user_control = json.load(f)
            
            disable_info = {
                "disabled_at": datetime.now().isoformat(),
                "reason": reason,
                "disabled_by": subprocess.run(['whoami'], capture_output=True, text=True).stdout.strip()
            }
            
            if hours:
                re_enable_time = datetime.now() + timedelta(hours=hours)
                disable_info["re_enable_at"] = re_enable_time.isoformat()
                print(f"User {username} disabled for {hours} hours")
            else:
                print(f"User {username} disabled until manually re-enabled")
            
            user_control["disabled_users"][username] = disable_info
            
            # Save state
            self.user_control_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.user_control_path, 'w') as f:
                json.dump(user_control, f, indent=2)
            
            # Kill user sessions
            subprocess.run(['sudo', 'pkill', '-u', username], check=False)
            subprocess.run(['sudo', 'loginctl', 'terminate-user', username], check=False)
            
            print(f"Account disabled and sessions terminated for: {username}")
            print(f"Reason: {reason}")
            
        except Exception as e:
            print(f"Error disabling user: {e}")
    
    def enable_user_account(self, username: str):
        """Enable a user account"""
        try:
            # Unlock the account
            result = subprocess.run(['sudo', 'passwd', '-u', username], 
                                  capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error: Failed to enable account: {result.stderr}")
                return
            
            # Update user control state
            if self.user_control_path.exists():
                with open(self.user_control_path, 'r') as f:
                    user_control = json.load(f)
                
                if username in user_control.get("disabled_users", {}):
                    del user_control["disabled_users"][username]
                    
                    with open(self.user_control_path, 'w') as f:
                        json.dump(user_control, f, indent=2)
            
            print(f"User account enabled: {username}")
            
        except Exception as e:
            print(f"Error enabling user: {e}")
    
    def _validate_hours(self, start_hour: int, end_hour: int) -> bool:
        """Validate hour range for schedule"""
        if not (0 <= start_hour <= 23):
            print(f"Error: Start hour must be between 0 and 23, got {start_hour}")
            return False
        if not (0 <= end_hour <= 23):
            print(f"Error: End hour must be between 0 and 23, got {end_hour}")
            return False
        if start_hour == end_hour:
            print(f"Error: Start and end hours cannot be the same")
            return False
        return True

    def _load_user_control(self) -> dict:
        """Load user control state"""
        defaults = {"disabled_users": {}, "scheduled_disables": {}, "daily_schedules": {}}
        try:
            if self.user_control_path.exists():
                with open(self.user_control_path, 'r') as f:
                    user_control = json.load(f)
                # Ensure required keys exist
                for key, default_value in defaults.items():
                    if key not in user_control:
                        user_control[key] = default_value
                return user_control
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in user control file: {e}")
            sys.exit(1)
        return defaults

    def _save_user_control(self, user_control: dict):
        """Save user control state and enable user control in config"""
        self.user_control_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.user_control_path, 'w') as f:
            json.dump(user_control, f, indent=2)

        # Enable user control in main config
        config = self.load_config()
        if "user_control" not in config:
            config["user_control"] = {}
        config["user_control"]["enabled"] = True
        self.save_config(config)

    def _format_hour_range(self, start_hour: int, end_hour: int) -> str:
        """Format hour range for display"""
        if start_hour > end_hour:
            return f"{start_hour:02d}:00 - {end_hour:02d}:00 (overnight)"
        return f"{start_hour:02d}:00 - {end_hour:02d}:00"

    def _normalize_schedule(self, schedule: dict) -> dict:
        """Normalize schedule to the array format, handling all legacy formats.

        Returns schedule in format:
        {
            "weekday": [{"start_hour": 8, "end_hour": 22}, ...],
            "weekend": [{"start_hour": 10, "end_hour": 23}, ...]
        }
        """
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

    def _format_windows(self, windows: list) -> str:
        """Format a list of time windows for display"""
        if not windows:
            return "No access"
        return ", ".join(self._format_hour_range(w["start_hour"], w["end_hour"]) for w in windows)

    def _print_schedule(self, schedule: dict, indent: str = ""):
        """Print schedule in a user-friendly format, handling all formats"""
        normalized = self._normalize_schedule(schedule)
        weekday_windows = normalized.get("weekday", [])
        weekend_windows = normalized.get("weekend", [])

        weekday_str = self._format_windows(weekday_windows)
        weekend_str = self._format_windows(weekend_windows)

        if weekday_windows == weekend_windows:
            if len(weekday_windows) == 0:
                print(f"{indent}Access Hours: No access (all days)")
            elif len(weekday_windows) == 1:
                print(f"{indent}Access Hours: {weekday_str} (all days)")
            else:
                print(f"{indent}Access Hours (all days):")
                for w in weekday_windows:
                    print(f"{indent}  {self._format_hour_range(w['start_hour'], w['end_hour'])}")
        else:
            print(f"{indent}Access Hours:")
            if len(weekday_windows) <= 1:
                print(f"{indent}  Weekday (Mon-Fri): {weekday_str}")
            else:
                print(f"{indent}  Weekday (Mon-Fri):")
                for w in weekday_windows:
                    print(f"{indent}    {self._format_hour_range(w['start_hour'], w['end_hour'])}")
            if len(weekend_windows) <= 1:
                print(f"{indent}  Weekend (Sat-Sun): {weekend_str}")
            else:
                print(f"{indent}  Weekend (Sat-Sun):")
                for w in weekend_windows:
                    print(f"{indent}    {self._format_hour_range(w['start_hour'], w['end_hour'])}")

    def set_weekday_hours(self, username: str, start_hour: int, end_hour: int):
        """Set allowed weekday (Mon-Fri) access hours for a user (replaces all windows)"""
        try:
            if not self._validate_hours(start_hour, end_hour):
                return

            user_control = self._load_user_control()

            # Get or create user schedule and normalize to array format
            if username not in user_control["daily_schedules"]:
                user_control["daily_schedules"][username] = {"weekday": [], "weekend": []}

            schedule = self._normalize_schedule(user_control["daily_schedules"][username])

            # Set weekday schedule (single window, replaces all)
            window = {"start_hour": start_hour, "end_hour": end_hour}
            schedule["weekday"] = [window]

            # Initialize weekend to same as weekday if empty
            if not schedule.get("weekend"):
                schedule["weekend"] = [window.copy()]

            user_control["daily_schedules"][username] = schedule
            self._save_user_control(user_control)

            print(f"Set weekday access hours for {username}: {self._format_hour_range(start_hour, end_hour)}")
            print("User will be automatically logged out outside these hours (Mon-Fri)")

        except Exception as e:
            print(f"Error setting weekday hours: {e}")

    def set_weekend_hours(self, username: str, start_hour: int, end_hour: int):
        """Set allowed weekend (Sat-Sun) access hours for a user (replaces all windows)"""
        try:
            if not self._validate_hours(start_hour, end_hour):
                return

            user_control = self._load_user_control()

            # Get or create user schedule and normalize to array format
            if username not in user_control["daily_schedules"]:
                user_control["daily_schedules"][username] = {"weekday": [], "weekend": []}

            schedule = self._normalize_schedule(user_control["daily_schedules"][username])

            # Set weekend schedule (single window, replaces all)
            window = {"start_hour": start_hour, "end_hour": end_hour}
            schedule["weekend"] = [window]

            # Initialize weekday to same as weekend if empty
            if not schedule.get("weekday"):
                schedule["weekday"] = [window.copy()]

            user_control["daily_schedules"][username] = schedule
            self._save_user_control(user_control)

            print(f"Set weekend access hours for {username}: {self._format_hour_range(start_hour, end_hour)}")
            print("User will be automatically logged out outside these hours (Sat-Sun)")

        except Exception as e:
            print(f"Error setting weekend hours: {e}")

    def add_weekday_window(self, username: str, start_hour: int, end_hour: int):
        """Add an additional weekday access window for a user"""
        try:
            if not self._validate_hours(start_hour, end_hour):
                return

            user_control = self._load_user_control()

            if username not in user_control["daily_schedules"]:
                user_control["daily_schedules"][username] = {"weekday": [], "weekend": []}

            schedule = self._normalize_schedule(user_control["daily_schedules"][username])
            window = {"start_hour": start_hour, "end_hour": end_hour}

            # Check for duplicate
            if window in schedule["weekday"]:
                print(f"Window {self._format_hour_range(start_hour, end_hour)} already exists for weekdays")
                return

            schedule["weekday"].append(window)
            # Sort windows by start hour for cleaner display
            schedule["weekday"].sort(key=lambda w: w["start_hour"])

            user_control["daily_schedules"][username] = schedule
            self._save_user_control(user_control)

            print(f"Added weekday window for {username}: {self._format_hour_range(start_hour, end_hour)}")
            print(f"Weekday windows: {self._format_windows(schedule['weekday'])}")

        except Exception as e:
            print(f"Error adding weekday window: {e}")

    def add_weekend_window(self, username: str, start_hour: int, end_hour: int):
        """Add an additional weekend access window for a user"""
        try:
            if not self._validate_hours(start_hour, end_hour):
                return

            user_control = self._load_user_control()

            if username not in user_control["daily_schedules"]:
                user_control["daily_schedules"][username] = {"weekday": [], "weekend": []}

            schedule = self._normalize_schedule(user_control["daily_schedules"][username])
            window = {"start_hour": start_hour, "end_hour": end_hour}

            # Check for duplicate
            if window in schedule["weekend"]:
                print(f"Window {self._format_hour_range(start_hour, end_hour)} already exists for weekends")
                return

            schedule["weekend"].append(window)
            # Sort windows by start hour for cleaner display
            schedule["weekend"].sort(key=lambda w: w["start_hour"])

            user_control["daily_schedules"][username] = schedule
            self._save_user_control(user_control)

            print(f"Added weekend window for {username}: {self._format_hour_range(start_hour, end_hour)}")
            print(f"Weekend windows: {self._format_windows(schedule['weekend'])}")

        except Exception as e:
            print(f"Error adding weekend window: {e}")

    def remove_weekday_window(self, username: str, start_hour: int, end_hour: int):
        """Remove a weekday access window for a user"""
        try:
            user_control = self._load_user_control()

            if username not in user_control["daily_schedules"]:
                print(f"No schedule found for user {username}")
                return

            schedule = self._normalize_schedule(user_control["daily_schedules"][username])
            window = {"start_hour": start_hour, "end_hour": end_hour}

            if window not in schedule["weekday"]:
                print(f"Window {self._format_hour_range(start_hour, end_hour)} not found in weekday schedule")
                return

            schedule["weekday"].remove(window)
            user_control["daily_schedules"][username] = schedule
            self._save_user_control(user_control)

            print(f"Removed weekday window for {username}: {self._format_hour_range(start_hour, end_hour)}")
            if schedule["weekday"]:
                print(f"Remaining weekday windows: {self._format_windows(schedule['weekday'])}")
            else:
                print("Warning: No weekday windows remaining - user will have no weekday access!")

        except Exception as e:
            print(f"Error removing weekday window: {e}")

    def remove_weekend_window(self, username: str, start_hour: int, end_hour: int):
        """Remove a weekend access window for a user"""
        try:
            user_control = self._load_user_control()

            if username not in user_control["daily_schedules"]:
                print(f"No schedule found for user {username}")
                return

            schedule = self._normalize_schedule(user_control["daily_schedules"][username])
            window = {"start_hour": start_hour, "end_hour": end_hour}

            if window not in schedule["weekend"]:
                print(f"Window {self._format_hour_range(start_hour, end_hour)} not found in weekend schedule")
                return

            schedule["weekend"].remove(window)
            user_control["daily_schedules"][username] = schedule
            self._save_user_control(user_control)

            print(f"Removed weekend window for {username}: {self._format_hour_range(start_hour, end_hour)}")
            if schedule["weekend"]:
                print(f"Remaining weekend windows: {self._format_windows(schedule['weekend'])}")
            else:
                print("Warning: No weekend windows remaining - user will have no weekend access!")

        except Exception as e:
            print(f"Error removing weekend window: {e}")

    def set_user_hours(self, username: str, start_hour: int, end_hour: int):
        """Set allowed access hours for a user (backward-compatible, sets weekday hours)"""
        self.set_weekday_hours(username, start_hour, end_hour)
    
    def show_user_status(self, username: str = None):
        """Show user account status"""
        try:
            user_control = {"disabled_users": {}, "scheduled_disables": {}, "daily_schedules": {}}
            if self.user_control_path.exists():
                with open(self.user_control_path, 'r') as f:
                    user_control = json.load(f)
            
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
                    schedule = user_control["daily_schedules"][username]
                    self._print_schedule(schedule)
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
                    for user, schedule in schedules.items():
                        print(f"\n{user}:")
                        self._print_schedule(schedule, indent="  ")
                        
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

    # Legacy command (alias for weekday)
    user_hours_parser = subparsers.add_parser('set-user-hours', help='Set access hours (alias for set-weekday-hours)')
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

    user_status_parser = subparsers.add_parser('user-status', help='Show user account status')
    user_status_parser.add_argument('username', nargs='?', help='Username (show all if not specified)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
        
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
    elif args.command == 'set-weekday-hours':
        manager.set_weekday_hours(args.username, args.start, args.end)
    elif args.command == 'set-weekend-hours':
        manager.set_weekend_hours(args.username, args.start, args.end)
    elif args.command == 'set-user-hours':
        manager.set_user_hours(args.username, args.start, args.end)
    elif args.command == 'add-weekday-window':
        manager.add_weekday_window(args.username, args.start, args.end)
    elif args.command == 'add-weekend-window':
        manager.add_weekend_window(args.username, args.start, args.end)
    elif args.command == 'remove-weekday-window':
        manager.remove_weekday_window(args.username, args.start, args.end)
    elif args.command == 'remove-weekend-window':
        manager.remove_weekend_window(args.username, args.start, args.end)
    elif args.command == 'user-status':
        manager.show_user_status(args.username)

if __name__ == "__main__":
    main()
