# Child Minder - Quick Reference Guide

## Installation

```bash
# 1. Download all files to a directory
# 2. Run installer
sudo bash install.sh

# 3. Configure
sudo nano /etc/child-minder/config.json

# 4. Start service
sudo systemctl start child-minder
```

## Service Management

```bash
# Start/Stop/Restart
sudo systemctl start child-minder
sudo systemctl stop child-minder
sudo systemctl restart child-minder

# Check status
sudo systemctl status child-minder

# View logs
sudo journalctl -u child-minder -f        # Live logs
sudo tail -f /var/log/child-minder/minder.log  # File logs
```

## Configuration Management (`cmctl`)

### View Current Setup
```bash
sudo cmctl config    # Show all settings
sudo cmctl usage     # Show today's usage
sudo cmctl status    # Service status
sudo cmctl logs      # Recent log entries
```

### Block/Unblock Applications
```bash
sudo cmctl block discord       # Block Discord completely
sudo cmctl unblock discord     # Remove block
```

### Individual Time Limits
```bash
sudo cmctl limit minecraft 60      # 60 minutes per day
sudo cmctl limit firefox 120       # 2 hours per day
sudo cmctl unlimit minecraft        # Remove limit
```

### Group Management
```bash
# Create/modify groups
sudo cmctl add-to-group games minecraft
sudo cmctl add-to-group games steam
sudo cmctl add-to-group games roblox

# Set group limits (applies to all apps in group)
sudo cmctl group-limit games 120    # 2 hours for all games combined
sudo cmctl group-limit browsers 180 # 3 hours for all browsers

# View groups
sudo cmctl groups

# Remove from group
sudo cmctl remove-from-group games minecraft

# Remove group limit
sudo cmctl group-unlimit games
```

### User Management
```bash
# Add/remove monitored users
sudo cmctl add-user johnny
sudo cmctl remove-user johnny

# Disable user account (immediate logout)
sudo cmctl disable-user johnny -r "Not doing homework"
sudo cmctl disable-user johnny -t 2 -r "2 hour timeout"  # Auto re-enable

# Re-enable user
sudo cmctl enable-user johnny

# Check user status
sudo cmctl user-status johnny
sudo cmctl user-status  # Show all disabled users
```

### Access Schedules (Weekday/Weekend)
```bash
# Set weekday hours (replaces all weekday windows)
sudo cmctl set-weekday-hours johnny 15 21    # Mon-Fri: 3 PM - 9 PM

# Set weekend hours (replaces all weekend windows)
sudo cmctl set-weekend-hours johnny 9 22     # Sat-Sun: 9 AM - 10 PM

# Legacy alias (same as set-weekday-hours)
sudo cmctl set-user-hours johnny 8 21

# Add extra time windows (without replacing existing ones)
sudo cmctl add-weekday-window johnny 7 8     # Add 7-8 AM weekday window
sudo cmctl add-weekend-window johnny 12 14   # Add 12-2 PM weekend window

# Remove specific windows
sudo cmctl remove-weekday-window johnny 7 8
sudo cmctl remove-weekend-window johnny 12 14

# Overnight schedules (start > end)
sudo cmctl set-weekend-hours johnny 22 6     # 10 PM - 6 AM
```

### System Control
```bash
sudo cmctl enable     # Enable monitoring
sudo cmctl disable    # Disable monitoring (temporary)
sudo cmctl reset      # Reset daily usage counters
```

## Common Scenarios

### Initial Setup for Child
```bash
# 1. Add child's username
sudo cmctl add-user johnny

# 2. Block inappropriate apps
sudo cmctl block discord
sudo cmctl block telegram

# 3. Set up game limits
sudo cmctl add-to-group games minecraft
sudo cmctl add-to-group games steam
sudo cmctl group-limit games 120  # 2 hours total

# 4. Set browser limits
sudo cmctl limit firefox 180  # 3 hours

# 5. Set school day hours (3 PM to 8 PM)
sudo cmctl set-weekday-hours johnny 15 20
```

### Weekend vs Weekday Rules
```bash
# Weekday hours (strict)
sudo cmctl set-weekday-hours johnny 15 20    # 3 PM - 8 PM

# Weekend hours (relaxed)
sudo cmctl set-weekend-hours johnny 8 22     # 8 AM - 10 PM
```

### Quick Punishments
```bash
# 30-minute timeout
sudo cmctl disable-user johnny -t 0.5 -r "Broke rules"

# 2-hour timeout
sudo cmctl disable-user johnny -t 2 -r "Not listening"

# Rest of the day
sudo cmctl disable-user johnny -r "Done for today"
# (Re-enable manually tomorrow with: sudo cmctl enable-user johnny)
```

### Emergency Override
```bash
# Temporarily stop monitoring
sudo systemctl stop child-minder

# Give extra time for special occasion
sudo cmctl limit minecraft 180  # Triple time today
# Remember to change back tomorrow!

# Complete disable for vacation
sudo cmctl disable
# Re-enable later with: sudo cmctl enable
```

## Troubleshooting

### Service Won't Start
```bash
# Check for errors
sudo journalctl -u child-minder -n 50

# Validate config
python3 -c "import json; json.load(open('/etc/child-minder/config.json'))"

# Check Python modules
python3 -c "import psutil"
```

### Notifications Not Working
```bash
# Test as user
sudo -u johnny DISPLAY=:0 notify-send "Test" "Message"

# Install notification support
sudo apt install libnotify-bin dbus-x11
```

### Process Not Being Blocked
```bash
# Check exact process name
ps aux | grep -i appname

# Check if user is monitored
sudo cmctl config | grep monitored_users

# Check logs for errors
sudo cmctl logs -n 100
```

### User Can Still Login When Disabled
```bash
# Force logout all sessions
sudo loginctl terminate-user johnny
sudo pkill -u johnny

# Verify account is locked
sudo passwd -S johnny  # Should show 'L'
```

## Configuration File Structure

```json
{
  "enabled": true,
  "check_interval": 5,
  "monitored_users": ["johnny", "sarah"],
  "blocked_processes": ["discord", "telegram"],
  "process_groups": {
    "games": ["minecraft", "steam", "roblox"],
    "browsers": ["firefox", "chrome", "chromium"],
    "social": ["discord", "telegram", "slack"]
  },
  "group_limits": {
    "games": 120,
    "browsers": 180,
    "social": 60
  },
  "limited_processes": {
    "minecraft": 60,
    "youtube": 45
  },
  "monitored_processes": ["firefox", "chrome", "minecraft"],
  "warning_time": 300,
  "warning_intervals": [1800, 900, 600, 300, 120, 60],
  "usage_log_interval": 300,
  "user_control": {
    "enabled": true,
    "check_interval": 60
  }
}
```

## Important Files

- **Config**: `/etc/child-minder/config.json`
- **State**: `/var/lib/child-minder/state.json`
- **User Control**: `/var/lib/child-minder/user_control.json`
- **Logs**: `/var/log/child-minder/minder.log`
- **Service**: `/etc/systemd/system/child-minder.service`
- **Scripts**: `/usr/bin/child-minder.py`, `/usr/bin/cmctl.py`

## Safety Commands

```bash
# Complete uninstall (interactive, with optional backup)
sudo bash uninstall.sh

# Backup configuration
sudo cp -r /etc/child-minder /etc/child-minder.backup

# Emergency: re-enable all users
for user in $(sudo cmctl user-status | grep "^[a-z]" | cut -d: -f1); do
    sudo cmctl enable-user $user
done
```