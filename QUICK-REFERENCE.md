# Process Monitor - Quick Reference Guide

## Installation

```bash
# 1. Download all files to a directory
# 2. Run installer
sudo bash install.sh

# 3. Configure
sudo nano /etc/process-monitor/config.json

# 4. Start service
sudo systemctl start process-monitor
```

## Service Management

```bash
# Start/Stop/Restart
sudo systemctl start process-monitor
sudo systemctl stop process-monitor
sudo systemctl restart process-monitor

# Check status
sudo systemctl status process-monitor

# View logs
sudo journalctl -u process-monitor -f        # Live logs
sudo tail -f /var/log/process-monitor/monitor.log  # File logs
```

## Configuration Management (`pmctl`)

### View Current Setup
```bash
sudo pmctl config    # Show all settings
sudo pmctl usage     # Show today's usage
sudo pmctl status    # Service status
sudo pmctl logs      # Recent log entries
```

### Block/Unblock Applications
```bash
sudo pmctl block discord       # Block Discord completely
sudo pmctl unblock discord     # Remove block
```

### Individual Time Limits
```bash
sudo pmctl limit minecraft 60      # 60 minutes per day
sudo pmctl limit firefox 120       # 2 hours per day
sudo pmctl unlimit minecraft        # Remove limit
```

### Group Management
```bash
# Create/modify groups
sudo pmctl add-to-group games minecraft
sudo pmctl add-to-group games steam
sudo pmctl add-to-group games roblox

# Set group limits (applies to all apps in group)
sudo pmctl group-limit games 120    # 2 hours for all games combined
sudo pmctl group-limit browsers 180 # 3 hours for all browsers

# View groups
sudo pmctl groups

# Remove from group
sudo pmctl remove-from-group games minecraft

# Remove group limit
sudo pmctl group-unlimit games
```

### User Management
```bash
# Add/remove monitored users
sudo pmctl add-user johnny
sudo pmctl remove-user johnny

# Disable user account (immediate logout)
sudo pmctl disable-user johnny -r "Not doing homework"
sudo pmctl disable-user johnny -t 2 -r "2 hour timeout"  # Auto re-enable

# Re-enable user
sudo pmctl enable-user johnny

# Set access hours (e.g., 8 AM to 9 PM)
sudo pmctl set-user-hours johnny 8 21

# Check user status
sudo pmctl user-status johnny
sudo pmctl user-status  # Show all disabled users
```

### System Control
```bash
sudo pmctl enable     # Enable monitoring
sudo pmctl disable    # Disable monitoring (temporary)
sudo pmctl reset      # Reset daily usage counters
```

## Common Scenarios

### Initial Setup for Child
```bash
# 1. Add child's username
sudo pmctl add-user johnny

# 2. Block inappropriate apps
sudo pmctl block discord
sudo pmctl block telegram

# 3. Set up game limits
sudo pmctl add-to-group games minecraft
sudo pmctl add-to-group games steam
sudo pmctl group-limit games 120  # 2 hours total

# 4. Set browser limits
sudo pmctl limit firefox 180  # 3 hours

# 5. Set school day hours (3 PM to 8 PM)
sudo pmctl set-user-hours johnny 15 20
```

### Weekend vs Weekday Rules
```bash
# Weekday (strict)
sudo pmctl group-limit games 60
sudo pmctl set-user-hours johnny 15 20

# Weekend (relaxed)
sudo pmctl group-limit games 180
sudo pmctl set-user-hours johnny 8 22
```

### Quick Punishments
```bash
# 30-minute timeout
sudo pmctl disable-user johnny -t 0.5 -r "Broke rules"

# 2-hour timeout
sudo pmctl disable-user johnny -t 2 -r "Not listening"

# Rest of the day
sudo pmctl disable-user johnny -r "Done for today"
# (Re-enable manually tomorrow with: sudo pmctl enable-user johnny)
```

### Emergency Override
```bash
# Temporarily stop monitoring
sudo systemctl stop process-monitor

# Give extra time for special occasion
sudo pmctl limit minecraft 180  # Triple time today
# Remember to change back tomorrow!

# Complete disable for vacation
sudo pmctl disable
# Re-enable later with: sudo pmctl enable
```

## Troubleshooting

### Service Won't Start
```bash
# Check for errors
sudo journalctl -u process-monitor -n 50

# Validate config
python3 -c "import json; json.load(open('/etc/process-monitor/config.json'))"

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
sudo pmctl config | grep monitored_users

# Check logs for errors
sudo pmctl logs -n 100
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

- **Config**: `/etc/process-monitor/config.json`
- **State**: `/var/lib/process-monitor/state.json`
- **User Control**: `/var/lib/process-monitor/user_control.json`
- **Logs**: `/var/log/process-monitor/monitor.log`
- **Service**: `/etc/systemd/system/process-monitor.service`
- **Scripts**: `/usr/local/bin/process-monitor.py`, `/usr/local/bin/pmctl.py`

## Safety Commands

```bash
# Complete uninstall
sudo systemctl stop process-monitor
sudo systemctl disable process-monitor
sudo rm -rf /etc/process-monitor
sudo rm -rf /var/lib/process-monitor
sudo rm -rf /var/log/process-monitor
sudo rm /usr/local/bin/process-monitor.py
sudo rm /usr/local/bin/pmctl*
sudo rm /etc/systemd/system/process-monitor.service
sudo systemctl daemon-reload

# Backup configuration
sudo cp -r /etc/process-monitor /etc/process-monitor.backup

# Emergency: re-enable all users
for user in $(sudo pmctl user-status | grep "^[a-z]" | cut -d: -f1); do
    sudo pmctl enable-user $user
done
```