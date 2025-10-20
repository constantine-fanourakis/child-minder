#!/usr/bin/env python3
"""
Process Monitor Management Utility
Provides easy management and reporting for the process monitor system
"""

import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
import subprocess
import pwd
import os

CONFIG_PATH = "/etc/process-monitor/config.json"
STATE_PATH = "/var/lib/process-monitor/state.json"
LOG_PATH = "/var/log/process-monitor/monitor.log"

class ProcessMonitorManager:
    def __init__(self):
        self.config_path = Path(CONFIG_PATH)
        self.state_path = Path(STATE_PATH)
        self.log_path = Path(LOG_PATH)
        self.user_control_path = Path("/var/lib/process-monitor/user_control.json")
        
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
            subprocess.run(['systemctl', 'reload', 'process-monitor'], check=False)
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
                    return state
        except Exception as e:
            print(f"Warning: Could not load state: {e}")
        return {"daily_usage": {}, "group_usage": {}, "last_reset": datetime.now().isoformat()}
        
    def add_blocked_process(self, process_name: str):
        """Add a process to the block list"""
        config = self.load_config()
        if process_name not in config['blocked_processes']:
            config['blocked_processes'].append(process_name)
            self.save_config(config)
            print(f"Added '{process_name}' to blocked processes")
        else:
            print(f"'{process_name}' is already blocked")
            
    def remove_blocked_process(self, process_name: str):
        """Remove a process from the block list"""
        config = self.load_config()
        if process_name in config['blocked_processes']:
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
        config = self.load_config()
        config['limited_processes'][process_name] = minutes
        self.save_config(config)
        print(f"Set time limit for '{process_name}' to {minutes} minutes")
        
    def remove_time_limit(self, process_name: str):
        """Remove time limit for a process"""
        config = self.load_config()
        if process_name in config['limited_processes']:
            del config['limited_processes'][process_name]
            self.save_config(config)
            print(f"Removed time limit for '{process_name}'")
        else:
            print(f"'{process_name}' has no time limit set")
            
    def add_monitored_user(self, username: str):
        """Add a user to monitor"""
        config = self.load_config()
        if username not in config['monitored_users']:
            config['monitored_users'].append(username)
            self.save_config(config)
            print(f"Added '{username}' to monitored users")
        else:
            print(f"'{username}' is already monitored")
            
    def remove_monitored_user(self, username: str):
        """Remove a user from monitoring"""
        config = self.load_config()
        if username in config['monitored_users']:
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
        
        if not daily_usage and not group_usage:
            print("No usage data available")
                    
    def reset_usage(self):
        """Reset usage statistics"""
        state = self.load_state()
        state['daily_usage'] = {}
        state['group_usage'] = {}
        state['last_reset'] = datetime.now().isoformat()
        
        try:
            with open(self.state_path, 'w') as f:
                json.dump(state, f, indent=2)
            print("Usage statistics reset successfully")
        except Exception as e:
            print(f"Error resetting usage: {e}")
            
    def service_status(self):
        """Show service status"""
        result = subprocess.run(['systemctl', 'status', 'process-monitor'], 
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
                            hours: int = None):
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
    
    def set_user_hours(self, username: str, start_hour: int, end_hour: int):
        """Set allowed access hours for a user"""
        try:
            # Load or create user control state
            user_control = {"disabled_users": {}, "scheduled_disables": {}, "daily_schedules": {}}
            if self.user_control_path.exists():
                with open(self.user_control_path, 'r') as f:
                    user_control = json.load(f)
            
            user_control["daily_schedules"][username] = {
                "start_hour": start_hour,
                "end_hour": end_hour,
                "weekday": True,
                "weekend": True
            }
            
            # Save state
            self.user_control_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.user_control_path, 'w') as f:
                json.dump(user_control, f, indent=2)
            
            # Enable user control in main config
            config = self.load_config()
            if "user_control" not in config:
                config["user_control"] = {}
            config["user_control"]["enabled"] = True
            self.save_config(config)
            
            print(f"Set access hours for {username}: {start_hour}:00 - {end_hour}:00")
            print("User will be automatically logged out outside these hours")
            
        except Exception as e:
            print(f"Error setting user hours: {e}")
    
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
                    print(f"Access Hours: {schedule['start_hour']}:00 - {schedule['end_hour']}:00")
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
                        print(f"{user}: {schedule['start_hour']}:00 - {schedule['end_hour']}:00")
                        
        except Exception as e:
            print(f"Error showing user status: {e}")

def main():
    parser = argparse.ArgumentParser(description='Process Monitor Management Utility')
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
    
    subparsers.add_parser('groups', help='List all process groups')
    
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
    subparsers.add_parser('enable', help='Enable monitoring')
    subparsers.add_parser('disable', help='Disable monitoring')
    
    # User account control
    disable_user_parser = subparsers.add_parser('disable-user', help='Disable a user account')
    disable_user_parser.add_argument('username', help='Username to disable')
    disable_user_parser.add_argument('-r', '--reason', default='Administrative action', help='Reason for disabling')
    disable_user_parser.add_argument('-t', '--hours', type=int, help='Duration in hours (permanent if not set)')
    
    enable_user_parser = subparsers.add_parser('enable-user', help='Enable a user account')
    enable_user_parser.add_argument('username', help='Username to enable')
    
    user_hours_parser = subparsers.add_parser('set-user-hours', help='Set allowed access hours for user')
    user_hours_parser.add_argument('username', help='Username')
    user_hours_parser.add_argument('start', type=int, help='Start hour (0-23)')
    user_hours_parser.add_argument('end', type=int, help='End hour (0-23)')
    
    user_status_parser = subparsers.add_parser('user-status', help='Show user account status')
    user_status_parser.add_argument('username', nargs='?', help='Username (show all if not specified)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
        
    manager = ProcessMonitorManager()
    
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
    elif args.command == 'groups':
        manager.list_groups()
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
    elif args.command == 'set-user-hours':
        manager.set_user_hours(args.username, args.start, args.end)
    elif args.command == 'user-status':
        manager.show_user_status(args.username)

if __name__ == "__main__":
    main()
