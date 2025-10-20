#!/usr/bin/env python3
"""
Process Monitor and Control System
A parental control system for monitoring and limiting application usage on Linux
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
import psutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set, Optional
import pwd
import subprocess

# Default paths
DEFAULT_CONFIG_PATH = "/etc/process-monitor/config.json"
DEFAULT_STATE_PATH = "/var/lib/process-monitor/state.json"
DEFAULT_LOG_PATH = "/var/log/process-monitor/monitor.log"

class ProcessMonitor:
    def __init__(self, config_path: str, state_path: str, log_path: str):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        
        # Ensure directories exist
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Setup logging
        self.setup_logging()
        
        # Load configuration
        self.config = self.load_config()
        
        # Load or initialize state
        self.state = self.load_state()
        
        # Track daily usage
        self.daily_usage: Dict[str, Dict[str, float]] = {}
        
        # Track group usage
        self.group_usage: Dict[str, Dict[str, float]] = {}
        
        # Track which warnings have been sent
        self.warned_processes: Dict[int, Set[int]] = {}  # pid -> set of warning times
        self.warned_groups: Dict[str, Dict[str, Set[int]]] = {}  # group -> user -> warning times
        
        # User control state file
        self.user_control_file = Path("/var/lib/process-monitor/user_control.json")
        self.user_control_state = self.load_user_control_state()
        
        # Running flag
        self.running = True
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
        
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(self.log_path),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
        self.save_state()
        sys.exit(0)
        
    def load_config(self) -> dict:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                self.logger.info("Configuration loaded successfully")
                return config
        except FileNotFoundError:
            self.logger.error(f"Config file not found at {self.config_path}")
            # Return default configuration
            return self.get_default_config()
        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing config file: {e}")
            sys.exit(1)
            
    def get_default_config(self) -> dict:
        """Return default configuration"""
        return {
            "enabled": True,
            "check_interval": 5,
            "monitored_users": [],
            "blocked_processes": [],
            "limited_processes": {},
            "process_groups": {},
            "group_limits": {},
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
                    return state
        except Exception as e:
            self.logger.warning(f"Could not load state: {e}")
        
        return {
            "daily_usage": {},
            "group_usage": {},
            "last_reset": datetime.now().isoformat()
        }
        
    def save_state(self):
        """Save current state to file"""
        try:
            state = {
                "daily_usage": self.serialize_usage(),
                "group_usage": self.group_usage,
                "last_reset": self.state.get("last_reset", datetime.now().isoformat())
            }
            with open(self.state_path, 'w') as f:
                json.dump(state, f, indent=2)
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
                
    def reset_daily_usage(self):
        """Reset daily usage counters"""
        last_reset = datetime.fromisoformat(self.state.get("last_reset", datetime.now().isoformat()))
        if datetime.now().date() > last_reset.date():
            self.logger.info("Resetting daily usage counters")
            self.daily_usage = {}
            self.group_usage = {}
            self.warned_processes = {}
            self.warned_groups = {}
            self.state["last_reset"] = datetime.now().isoformat()
            self.save_state()
            
    def get_username(self, uid: int) -> Optional[str]:
        """Get username from UID"""
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None
            
    def should_monitor_user(self, username: str) -> bool:
        """Check if user should be monitored"""
        monitored_users = self.config.get("monitored_users", [])
        if not monitored_users:  # If empty, monitor all users
            return True
        return username in monitored_users
        
    def is_process_blocked(self, process_name: str) -> bool:
        """Check if process is in block list with improved matching"""
        blocked = self.config.get("blocked_processes", [])
        process_lower = process_name.lower()
        process_base = os.path.basename(process_name).lower()
        
        for blocked_name in blocked:
            blocked_lower = blocked_name.lower()
            if blocked_lower in process_lower or blocked_lower in process_base:
                return True
        return False
        
    def get_process_group(self, process_name: str) -> Optional[str]:
        """Get the group name for a process, if any"""
        process_groups = self.config.get("process_groups", {})
        process_lower = process_name.lower()
        process_base = os.path.basename(process_name).lower()
        
        for group_name, processes in process_groups.items():
            for proc in processes:
                if proc.lower() in process_lower or proc.lower() in process_base:
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
        """Send warning to user about group time limit with improved methods"""
        try:
            # Determine urgency based on time
            urgency = 'critical' if remaining_minutes <= 5 else 'normal'
            title = "⚠️ Group Time Limit Warning" if remaining_minutes > 1 else "🚨 FINAL GROUP WARNING"
            
            # Build message
            message = (f"Group '{group_name}' has {remaining_minutes} minute{'s' if remaining_minutes > 1 else ''} remaining today. "
                      f"All apps in this group will close when time expires.")
            if remaining_minutes <= 2:
                message += " SAVE YOUR WORK NOW!"
            
            # Get user display
            display = self.get_user_display(username)
            
            # Try to get user ID for DBUS
            try:
                uid = pwd.getpwnam(username).pw_uid
                dbus_addr = f"unix:path=/run/user/{uid}/bus"
            except:
                dbus_addr = ""
            
            success = False
            
            # Method 1: notify-send with proper DBUS
            if dbus_addr and os.path.exists(f"/run/user/{uid}/bus"):
                cmd = (f'sudo -u {username} DISPLAY={display} '
                      f'DBUS_SESSION_BUS_ADDRESS={dbus_addr} '
                      f'notify-send -u {urgency} -t 10000 '
                      f'"{title}" "{message}"')
                result = subprocess.run(cmd, shell=True, capture_output=True, timeout=2)
                if result.returncode == 0:
                    success = True
            
            # Method 2: Try without DBUS
            if not success:
                cmd = f'sudo -u {username} DISPLAY={display} notify-send -u {urgency} "{title}" "{message}"'
                result = subprocess.run(cmd, shell=True, capture_output=True, timeout=2)
                if result.returncode == 0:
                    success = True
            
            # Method 3: Wall message for critical
            if remaining_minutes <= 2:
                subprocess.run(['wall', f"GROUP WARNING: {message}"], capture_output=True, timeout=2)
                success = True
            
            # Play sound for critical warnings
            if remaining_minutes <= 5:
                sound_files = [
                    '/usr/share/sounds/freedesktop/stereo/dialog-warning.oga',
                    '/usr/share/sounds/ubuntu/stereo/dialog-warning.ogg',
                    '/usr/share/sounds/gnome/default/alerts/glass.ogg'
                ]
                for sound in sound_files:
                    if os.path.exists(sound):
                        subprocess.run(f'sudo -u {username} DISPLAY={display} paplay {sound} 2>/dev/null &',
                                     shell=True, capture_output=True)
                        break
            
            if success:
                self.logger.info(f"Warned user {username} about group '{group_name}' time limit ({remaining_minutes} min remaining)")
            else:
                self.logger.warning(f"Could not send desktop notification for {username}, using wall")
                subprocess.run(['wall', f"GROUP WARNING: {group_name} has {remaining_minutes} minutes left"],
                             capture_output=True, timeout=2)
            
        except subprocess.TimeoutExpired:
            self.logger.error("Group warning command timed out")
        except Exception as e:
            self.logger.error(f"Error in group warning system: {e}")
            
        except Exception as e:
            self.logger.error(f"Error in group warning system: {e}")
    
    def get_process_limit(self, process_name: str) -> Optional[int]:
        """Get time limit for process in seconds"""
        limited = self.config.get("limited_processes", {})
        process_lower = process_name.lower()
        process_base = os.path.basename(process_name).lower()
        
        for limited_name, limit_minutes in limited.items():
            if limited_name.lower() in process_lower or limited_name.lower() in process_base:
                return limit_minutes * 60  # Convert to seconds
        return None
        
    def is_process_monitored(self, process_name: str) -> bool:
        """Check if process should be logged"""
        monitored = self.config.get("monitored_processes", [])
        if not monitored:  # If empty, don't log anything
            return False
        
        process_lower = process_name.lower()
        process_base = os.path.basename(process_name).lower()
        
        for mon_name in monitored:
            if mon_name.lower() in process_lower or mon_name.lower() in process_base:
                return True
        return False
        
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
        """Send warning to user about time limit with improved methods"""
        try:
            username = self.get_username(proc.uids().real)
            process_name = proc.name()
            
            # Determine urgency based on time
            urgency = 'critical' if remaining_minutes <= 5 else 'normal'
            title = "⚠️ Time Limit Warning" if remaining_minutes > 1 else "🚨 FINAL WARNING"
            
            # Build message
            message = f"{process_name} has {remaining_minutes} minute{'s' if remaining_minutes > 1 else ''} remaining today."
            if remaining_minutes <= 2:
                message += " SAVE YOUR WORK NOW!"
            
            # Get user display
            display = self.get_user_display(username)
            
            # Try to get user ID for DBUS
            try:
                uid = pwd.getpwnam(username).pw_uid
                dbus_addr = f"unix:path=/run/user/{uid}/bus"
            except:
                dbus_addr = ""
            
            success = False
            
            # Method 1: notify-send with proper DBUS
            if dbus_addr and os.path.exists(f"/run/user/{uid}/bus"):
                cmd = (f'sudo -u {username} DISPLAY={display} '
                      f'DBUS_SESSION_BUS_ADDRESS={dbus_addr} '
                      f'notify-send -u {urgency} -t 10000 '
                      f'"{title}" "{message}"')
                result = subprocess.run(cmd, shell=True, capture_output=True, timeout=2)
                if result.returncode == 0:
                    success = True
            
            # Method 2: Try without DBUS (some systems don't need it)
            if not success:
                cmd = f'sudo -u {username} DISPLAY={display} notify-send -u {urgency} "{title}" "{message}"'
                result = subprocess.run(cmd, shell=True, capture_output=True, timeout=2)
                if result.returncode == 0:
                    success = True
            
            # Method 3: Wall message for critical warnings
            if remaining_minutes <= 2:
                subprocess.run(['wall', f"WARNING: {message}"], capture_output=True, timeout=2)
                success = True
            
            # Method 4: Terminal message for active terminals
            if remaining_minutes <= 5:
                try:
                    for proc_iter in psutil.process_iter(['username', 'terminal']):
                        if proc_iter.username() == username and proc_iter.info['terminal']:
                            try:
                                tty = f"/dev/{proc_iter.info['terminal']}"
                                with open(tty, 'w') as t:
                                    t.write(f"\n*** {title} ***\n{message}\n\n")
                                success = True
                            except:
                                continue
                except:
                    pass
            
            # Play sound for critical warnings
            if remaining_minutes <= 5:
                sound_files = [
                    '/usr/share/sounds/freedesktop/stereo/dialog-warning.oga',
                    '/usr/share/sounds/ubuntu/stereo/dialog-warning.ogg',
                    '/usr/share/sounds/gnome/default/alerts/glass.ogg'
                ]
                for sound in sound_files:
                    if os.path.exists(sound):
                        subprocess.run(f'sudo -u {username} DISPLAY={display} paplay {sound} 2>/dev/null &', 
                                     shell=True, capture_output=True)
                        break
            
            if success:
                self.logger.info(f"Warned user {username} about {process_name} time limit ({remaining_minutes} min remaining)")
            else:
                self.logger.warning(f"Could not send desktop notification for {username}, using wall")
                subprocess.run(['wall', f"TIME WARNING: {process_name} has {remaining_minutes} minutes left"], 
                             capture_output=True, timeout=2)
            
        except subprocess.TimeoutExpired:
            self.logger.error("Warning command timed out")
        except Exception as e:
            self.logger.error(f"Error in warning system: {e}")
            
        except Exception as e:
            self.logger.error(f"Error in warning system: {e}")
    
    def get_user_display(self, username: str) -> str:
        """Get the user's display variable"""
        try:
            # Try to find display from user's processes
            for p in psutil.process_iter(['username', 'environ']):
                try:
                    if p.username() == username:
                        env = p.environ()
                        if 'DISPLAY' in env:
                            return env['DISPLAY']
                except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                    continue
        except:
            pass
        return ':0'  # Default fallback
            
    def monitor_processes(self):
        """Main monitoring loop iteration"""
        # First, check for disabled users if user control is enabled
        if self.config.get("user_control", {}).get("enabled", False):
            # Reload user control state
            self.user_control_state = self.load_user_control_state()
            
            # Handle any disabled users
            for username in self.user_control_state.get("disabled_users", {}):
                self.handle_disabled_user(username)
            
            # Check access hours for all monitored users
            for username in self.config.get("monitored_users", []):
                if not self.check_user_access_hours(username):
                    # User is outside allowed hours, treat as disabled
                    self.handle_disabled_user(username)
        
        # Regular process monitoring
        for proc in psutil.process_iter(['pid', 'name', 'username', 'uids', 'create_time']):
            try:
                # Get process info
                pinfo = proc.info
                
                # Skip kernel threads and system processes
                if pinfo['username'] is None:
                    continue
                    
                username = self.get_username(proc.uids().real)
                if not username or not self.should_monitor_user(username):
                    continue
                    
                process_name = pinfo['name']
                
                # Check if process is blocked
                if self.is_process_blocked(process_name):
                    self.terminate_process(proc, "Blocked application")
                    continue
                
                # Check which group this process belongs to, if any
                group_name = self.get_process_group(process_name)
                
                # Track whether to terminate due to limits
                should_terminate = False
                terminate_reason = ""
                
                # Check group limits first (if applicable)
                if group_name:
                    group_limit = self.get_group_limit(group_name)
                    if group_limit:
                        # Update group usage
                        self.update_group_usage(username, group_name, self.config['check_interval'])
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
                            
                # Log monitored processes
                if self.is_process_monitored(process_name):
                    self.update_usage(username, process_name, self.config['check_interval'])
                    # Also update group usage for monitoring
                    if group_name:
                        self.update_group_usage(username, group_name, self.config['check_interval'])
                    
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as e:
                self.logger.error(f"Error monitoring process: {e}")
                
    def log_usage_summary(self):
        """Log usage summary periodically"""
        if not self.daily_usage and not self.group_usage:
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
                        
        self.logger.info("===================")
        
    def reload_config(self):
        """Reload configuration from file"""
        self.logger.info("Reloading configuration...")
        self.config = self.load_config()
        
    def run(self):
        """Main monitoring loop"""
        self.logger.info("Process Monitor started")
        
        # Load saved usage data (with backward compatibility)
        if "daily_usage" in self.state:
            self.deserialize_usage(self.state["daily_usage"])
        if "group_usage" in self.state:
            self.group_usage = self.state["group_usage"]
            
        last_summary = time.time()
        last_save = time.time()
        
        while self.running:
            try:
                # Check if enabled
                if not self.config.get("enabled", True):
                    time.sleep(10)
                    continue
                    
                # Reset daily counters if needed
                self.reset_daily_usage()
                
                # Monitor processes
                self.monitor_processes()
                
                # Log summary periodically
                if time.time() - last_summary >= self.config.get('usage_log_interval', 60):
                    self.log_usage_summary()
                    last_summary = time.time()
                    
                # Save state periodically
                if time.time() - last_save >= 60:
                    self.save_state()
                    last_save = time.time()
                    
                # Sleep for check interval
                time.sleep(self.config.get('check_interval', 5))
                
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}")
                time.sleep(5)
                
        self.logger.info("Process Monitor stopped")

def main():
    parser = argparse.ArgumentParser(description='Process Monitor and Control System')
    parser.add_argument('--config', default=DEFAULT_CONFIG_PATH, help='Path to config file')
    parser.add_argument('--state', default=DEFAULT_STATE_PATH, help='Path to state file')
    parser.add_argument('--log', default=DEFAULT_LOG_PATH, help='Path to log file')
    
    args = parser.parse_args()
    
    monitor = ProcessMonitor(args.config, args.state, args.log)
    monitor.run()

if __name__ == "__main__":
    main()
