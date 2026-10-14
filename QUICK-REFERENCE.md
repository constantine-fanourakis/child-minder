# Child Minder - Quick Reference Guide

## Installation

```bash
# 1. Download all files to a directory
# 2. Run installer (starts the service)
sudo bash install.sh

# 3. Configure
sudo nano /etc/child-minder/config.json

# 4. Apply hand edits (cmctl changes reload automatically)
sudo systemctl reload child-minder
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

## Web UI

```bash
sudo cmctl web-password                     # Set the password first
# Then browse to http://<computer's address>:8765/
sudo systemctl status child-minder-web      # Web UI status
sudo journalctl -u 'child-minder-broker@*'  # Changes made from the web UI
# Open the port for the home network only (install.sh does not):
sudo nmcli connection modify "<home network name>" connection.zone home
sudo nmcli connection up "<home network name>"
sudo firewall-cmd --permanent --zone=home --add-port=8765/tcp && sudo firewall-cmd --reload
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

### Per-User Daily Screen Time
```bash
# Set total screen time cap (all monitored/limited apps count toward this)
sudo cmctl user-limit johnny 120           # 2 hours total, same for weekday & weekend
sudo cmctl user-weekday-limit johnny 60    # Weekdays: 1 hour total
sudo cmctl user-weekend-limit johnny 180   # Weekends: 3 hours total
sudo cmctl user-weekday-unlimit johnny     # Remove only the weekday limit
sudo cmctl user-weekend-unlimit johnny     # Remove only the weekend limit
sudo cmctl user-unlimit johnny             # Remove all daily limits for user
```

Daily screen time is recorded even without a daily limit. Adding a limit later
includes today's already-recorded usage; simultaneous tracked apps count once.

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
sudo cmctl add-user '*'          # Every regular user; an empty list monitors nobody

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

# Legacy command: sets weekday hours, and weekend hours if none are set
sudo cmctl set-user-hours johnny 8 21

# Add extra time windows (without replacing existing ones)
sudo cmctl add-weekday-window johnny 7 8     # Add 7-8 AM weekday window
sudo cmctl add-weekend-window johnny 12 14   # Add 12-2 PM weekend window

# Remove specific windows; removing the last one for a day type
# denies that day type
sudo cmctl remove-weekday-window johnny 7 8
sudo cmctl remove-weekend-window johnny 12 14

# Clear all windows for a day type, or the whole schedule
sudo cmctl clear-weekday-windows johnny      # weekdays unrestricted
sudo cmctl clear-weekend-windows johnny      # weekends unrestricted
sudo cmctl clear-user-hours johnny           # unrestricted at any time

# Deny a whole day type; set hours or clear to lift it
sudo cmctl deny-weekday-access johnny        # no access Mon-Fri
sudo cmctl deny-weekend-access johnny        # no access Sat-Sun

# Overnight schedules (start > end)
sudo cmctl set-weekend-hours johnny 22 6     # 10 PM - 6 AM
```

A day type with no windows is unrestricted; one that is denied has no access
at all. Weekday and weekend schedules are independent: setting one leaves the
other open until it is set too.

### System Control
```bash
sudo cmctl enable     # Enable monitoring
sudo cmctl disable    # Disable monitoring (temporary)
sudo cmctl reset      # Reset daily usage counters
sudo cmctl web-password   # Set the web UI password
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

# 5. Set overall daily screen time caps
sudo cmctl user-weekday-limit johnny 120  # 2 hours total on school days
sudo cmctl user-weekend-limit johnny 240  # 4 hours total on weekends

# 6. Set school day hours (3 PM to 8 PM)
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
sudo apt install libnotify-bin
```

### Process Not Being Blocked
```bash
# Check exact process name
ps aux | grep -i appname

# Check if user is monitored
sudo cmctl config | grep "Monitored Users"

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
  "min_uid": 1000,
  "monitored_users": ["johnny", "sarah"],
  "blocked_processes": ["discord*", "telegram*"],
  "process_groups": {
    "games": ["minecraft*", "steam*", "roblox*"],
    "browsers": ["firefox*", "chrome*", "chromium*"],
    "social": ["discord*", "telegram*", "slack*"]
  },
  "group_limits": {
    "games": 120,
    "browsers": 180,
    "social": 60
  },
  "user_daily_limits": {
    "johnny": {"weekday": 60, "weekend": 180},
    "sarah": {"weekday": 90, "weekend": 120}
  },
  "limited_processes": {
    "minecraft*": 60,
    "youtube*": 45
  },
  "monitored_processes": ["firefox*", "chrome*", "minecraft*"],
  "warning_time": 300,
  "warning_intervals": [1800, 900, 600, 300, 120, 60],
  "usage_log_interval": 60,
  "user_control": {
    "enabled": true,
    "check_interval": 60
  }
}
```

## Process Patterns

Patterns match a process name exactly, unless they contain `*`, `?` or `[`,
which makes them a glob. Matching is case-insensitive and never by substring, so
`sh` matches only `sh`, not `bash`.

```bash
ps -eo comm= | sort -u | grep -i steam   # find the real process names
```

Use a trailing `*` to catch helper processes: `steam*` covers `steamwebhelper`,
and every process matching one pattern shares a single time counter.

## Important Files

- **Config**: `/etc/child-minder/config.json`
- **State**: `/var/lib/child-minder/state.json`
- **User Control**: `/var/lib/child-minder/user_control.json`
- **Logs**: `/var/log/child-minder/minder.log` (rotates at 5 MB, 5 kept)
- **Last good config**: `/var/lib/child-minder/config.last-good.json`
- **Web UI settings and password hash**: `/etc/child-minder/web.json`
- **Service**: `/etc/systemd/system/child-minder.service`
- **Web UI units**: `child-minder-web.service`, `child-minder-broker.socket`,
  `child-minder-broker@.service` in `/etc/systemd/system/`
- **Program**: `/usr/lib/child-minder/` (`cmctl` is linked from `/usr/bin`)

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
