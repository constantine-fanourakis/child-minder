# Linux Process Monitor and Control System

A comprehensive parental control system for Linux that monitors and controls application usage, with features for blocking applications, setting time limits, and logging usage statistics.

## Features

- **Process Blocking**: Completely block specified applications from running
- **Time Limits**: Set daily time limits for individual applications
- **Group Limits**: Set combined time limits for groups of applications (e.g., all games = 2 hours total)
- **User Account Control**: Disable/enable user accounts and set access hours
- **Usage Logging**: Track and log application usage for specified programs
- **User-Specific Monitoring**: Monitor only specific user accounts
- **Automatic Warnings**: Warn users before their time runs out
- **Daily Reset**: Usage counters reset automatically each day
- **Service Management**: Runs as a system service with automatic startup
- **Easy Configuration**: JSON-based configuration with management utility

## Requirements

- Linux system with systemd
- Python 3.6 or higher
- Root/sudo access for installation
- Python package: `psutil`

## Installation

1. **Download all files** to a directory (e.g., `/tmp/process-monitor/`):
   - `process-monitor.py` - Main monitoring script
   - `config.json` - Configuration file
   - `process-monitor.service` - Systemd service file
   - `install.sh` - Installation script
   - `pmctl.py` - Management utility

2. **Make scripts executable**:
   ```bash
   chmod +x install.sh process-monitor.py pmctl.py
   ```

3. **Run the installation script**:
   ```bash
   sudo ./install.sh
   ```

4. **Configure the system**:
   ```bash
   sudo nano /etc/process-monitor/config.json
   ```
   Update the configuration with:
   - Your son's username in `monitored_users`
   - Applications to block in `blocked_processes`
   - Time limits in `limited_processes`
   - Applications to log in `monitored_processes`

5. **Start the service**:
   ```bash
   sudo systemctl start process-monitor
   ```

## Configuration

The configuration file (`/etc/process-monitor/config.json`) contains:

### Basic Settings
- `enabled`: Master on/off switch for monitoring
- `check_interval`: How often to check processes (seconds)
- `warning_time`: Single warning time before limit (seconds, default 300 = 5 minutes)
- `warning_intervals`: Multiple warning times (seconds) - e.g., [1800, 900, 600, 300, 120, 60] for warnings at 30, 15, 10, 5, 2, and 1 minute
- `usage_log_interval`: How often to log usage summary (seconds)

### User Management
- `monitored_users`: List of usernames to monitor (empty = all users)

### Process Control
- `blocked_processes`: List of process names to block completely
- `limited_processes`: Dictionary of process names with individual daily time limits (in minutes)
- `monitored_processes`: List of process names to track usage for

### Process Groups (New!)
- `process_groups`: Define groups of related applications
  - Example: Group all games together, all browsers together, etc.
- `group_limits`: Set total daily time limits for entire groups (in minutes)
  - Example: All games combined get 2 hours, regardless of which game

### Group Limits vs Individual Limits
When both group and individual limits are set:
- **Both limits are enforced** - whichever is reached first triggers
- Example: If "games" group has 120 minutes and "minecraft" has 60 minutes:
  - Playing only Minecraft: limited to 60 minutes
  - Playing multiple games: all games combined limited to 120 minutes
  - If 90 minutes spent on Steam, only 30 minutes left for any game in the group

### Example Configuration
```json
{
  "enabled": true,
  "check_interval": 5,
  "monitored_users": ["johnny"],
  "blocked_processes": ["discord", "telegram"],
  "process_groups": {
    "games": ["minecraft", "steam", "roblox", "fortnite"],
    "browsers": ["firefox", "chrome", "chromium", "brave"],
    "social": ["discord", "telegram", "slack"],
    "entertainment": ["vlc", "mpv", "spotify", "youtube"]
  },
  "group_limits": {
    "games": 120,
    "browsers": 180,
    "entertainment": 90
  },
  "limited_processes": {
    "minecraft": 60,
    "youtube": 45
  },
  "monitored_processes": ["firefox", "chrome", "minecraft", "steam"]
}
```

## Management Utility (pmctl.py)

The management utility provides easy command-line control:

### View Information
```bash
sudo ./pmctl.py config        # Show current configuration
sudo ./pmctl.py usage         # Show today's usage statistics
sudo ./pmctl.py status        # Show service status
sudo ./pmctl.py logs -n 100   # Show last 100 log entries
sudo ./pmctl.py groups        # List all process groups and limits
```

### Manage Blocked Processes
```bash
sudo ./pmctl.py block discord      # Block Discord
sudo ./pmctl.py unblock discord    # Unblock Discord
```

### Manage Individual Time Limits
```bash
sudo ./pmctl.py limit minecraft 60    # Limit Minecraft to 60 minutes/day
sudo ./pmctl.py unlimit minecraft     # Remove time limit
```

### Manage Process Groups
```bash
# Create/modify groups
sudo ./pmctl.py add-to-group games minecraft       # Add Minecraft to games group
sudo ./pmctl.py add-to-group games roblox         # Add Roblox to games group
sudo ./pmctl.py remove-from-group games minecraft  # Remove from group

# Set group limits
sudo ./pmctl.py group-limit games 120      # All games combined: 2 hours/day
sudo ./pmctl.py group-limit browsers 180   # All browsers combined: 3 hours/day
sudo ./pmctl.py group-unlimit games        # Remove group limit
```

### Manage Users
```bash
sudo ./pmctl.py add-user johnny       # Monitor user 'johnny'
sudo ./pmctl.py remove-user johnny    # Stop monitoring user 'johnny'
```

### Control Service
```bash
sudo ./pmctl.py enable     # Enable monitoring
sudo ./pmctl.py disable    # Disable monitoring (temporarily)
sudo ./pmctl.py reset      # Reset daily usage counters
```

### User Account Control
```bash
# Disable user account (immediate logout)
sudo ./pmctl.py disable-user johnny -r "Breaking rules"
sudo ./pmctl.py disable-user johnny -t 2 -r "2 hour timeout"  # Auto re-enable after 2 hours

# Re-enable user account
sudo ./pmctl.py enable-user johnny

# Set allowed access hours (e.g., 8 AM to 9 PM)
sudo ./pmctl.py set-user-hours johnny 8 21

# Check user status
sudo ./pmctl.py user-status johnny
sudo ./pmctl.py user-status         # Show all disabled users
```

## Service Management

### Basic Commands
```bash
sudo systemctl start process-monitor    # Start service
sudo systemctl stop process-monitor     # Stop service
sudo systemctl restart process-monitor  # Restart service
sudo systemctl status process-monitor   # Check status
sudo systemctl enable process-monitor   # Enable auto-start at boot
sudo systemctl disable process-monitor  # Disable auto-start
```

### View Logs
```bash
# System logs
sudo journalctl -u process-monitor -f   # Follow live logs
sudo journalctl -u process-monitor --since today

# Application logs
sudo tail -f /var/log/process-monitor/monitor.log
```

## How It Works

1. **Process Monitoring**: The service checks all running processes every 5 seconds
2. **User Filtering**: Only processes belonging to monitored users are checked
3. **Blocking**: Blocked processes are immediately terminated when detected
4. **Time Tracking**: Time-limited processes have their usage tracked throughout the day
5. **Group Tracking**: Processes in groups count toward both individual and group limits
6. **Warnings**: Users receive desktop notifications when approaching time limits
7. **Termination**: Processes are terminated when daily limits are exceeded (either individual or group)
8. **Logging**: All actions and usage statistics are logged to files
9. **Daily Reset**: Usage counters reset at midnight for the next day

## User Account Control

The system includes comprehensive user account control features for stronger parental control:

### Disable/Enable User Accounts

Completely disable a user account when needed:
```bash
# Disable immediately with reason
sudo ./pmctl.py disable-user johnny -r "Homework not completed"

# Disable for specific duration (auto re-enable)
sudo ./pmctl.py disable-user johnny -t 3 -r "3 hour timeout"

# Re-enable manually
sudo ./pmctl.py enable-user johnny
```

When an account is disabled:
- User is immediately logged out
- Account is locked (cannot log in)
- All running processes are terminated
- Attempts to log in show the disable reason

### Set Access Hours

Control when users can access the computer:
```bash
# Set allowed hours (24-hour format)
sudo ./pmctl.py set-user-hours johnny 8 21  # 8 AM to 9 PM

# School days: 3 PM to 9 PM
sudo ./pmctl.py set-user-hours johnny 15 21

# Weekends: 8 AM to 10 PM
sudo ./pmctl.py set-user-hours johnny 8 22
```

Outside allowed hours:
- User is automatically logged out
- Account is temporarily disabled
- Automatically re-enabled when allowed time begins

### User Account Control Examples

#### Example 1: School Night Routine
```bash
# Set school night hours (3 PM to 8 PM on weekdays)
sudo ./pmctl.py set-user-hours johnny 15 20

# If rules are broken, immediate timeout
sudo ./pmctl.py disable-user johnny -t 1 -r "Broke screen time rules"
```

#### Example 2: Weekend Punishment
```bash
# No computer for the weekend
sudo ./pmctl.py disable-user johnny -t 48 -r "Grounded from computer"
```

#### Example 3: Homework First Policy
```bash
# Disable until homework is verified
sudo ./pmctl.py disable-user johnny -r "Complete homework first"

# Parent checks homework, then:
sudo ./pmctl.py enable-user johnny
```

#### Example 4: Graduated Consequences
- First violation: Warning
- Second violation: 1-hour timeout
- Third violation: Rest of day disabled
- Fourth violation: Weekend disabled

### Checking User Status

Monitor account status:
```bash
# Check specific user
sudo ./pmctl.py user-status johnny

# Output:
# === User Status: johnny ===
# Status: DISABLED
#   Disabled at: 2024-01-15T15:30:00
#   Reason: Breaking rules
#   Will re-enable at: 2024-01-15T17:30:00
# Access Hours: 8:00 - 21:00

# Check all disabled users
sudo ./pmctl.py user-status
```

### Configuration for User Control

Enable user control in config.json:
```json
{
  "user_control": {
    "enabled": true,
    "check_interval": 60,
    "auto_disable_on_violations": true,
    "violation_threshold": 3
  }
}
```

### How User Control Works

1. **Account Locking**: Uses Linux `passwd -l` to lock accounts
2. **Session Termination**: Kills all user processes and login sessions
3. **Automatic Enforcement**: Checks every minute for violations
4. **Access Hours**: Automatically disables/enables based on time
5. **Logging**: All actions are logged for review

### Security Notes

- Only root/sudo can disable/enable accounts
- Disabled users cannot bypass with alternative login methods
- Account locking is system-level (affects SSH, console, GUI)
- Re-enabling requires parent/admin intervention

### Parent Tips for User Control

1. **Clear Communication**: Explain the rules and consequences
2. **Gradual Implementation**: Start with warnings before disabling
3. **Consistent Enforcement**: Apply rules consistently
4. **Time-Based Strategy**:
   - School nights: Strict hours (homework time to bedtime)
   - Weekends: More flexible hours
   - Holidays: Adjusted schedule
5. **Quick Timeouts**: Use short disables (30-60 min) for minor issues
6. **Emergency Override**: Keep admin password secure for emergencies

## Group Limiting Examples

### Example 1: Gaming Limits
Set up a 2-hour total limit for all games:
```bash
# Define the games group
sudo ./pmctl.py add-to-group games minecraft
sudo ./pmctl.py add-to-group games steam
sudo ./pmctl.py add-to-group games roblox
sudo ./pmctl.py add-to-group games fortnite

# Set 2-hour group limit
sudo ./pmctl.py group-limit games 120

# Optional: Set stricter limits for specific games
sudo ./pmctl.py limit fortnite 30  # Fortnite specifically limited to 30 min
```

Result: User can play any combination of games for 2 hours total. Fortnite specifically can't exceed 30 minutes.

### Example 2: Educational vs Entertainment
Balance educational and entertainment content:
```bash
# Entertainment group (1.5 hours)
sudo ./pmctl.py add-to-group entertainment youtube
sudo ./pmctl.py add-to-group entertainment netflix
sudo ./pmctl.py add-to-group entertainment vlc
sudo ./pmctl.py group-limit entertainment 90

# Educational group (3 hours)
sudo ./pmctl.py add-to-group educational khan-academy
sudo ./pmctl.py add-to-group educational duolingo
sudo ./pmctl.py group-limit educational 180
```

### Example 3: Social Media Control
Limit all social media to 1 hour combined:
```bash
# Create social media group
sudo ./pmctl.py add-to-group social discord
sudo ./pmctl.py add-to-group social telegram
sudo ./pmctl.py add-to-group social slack
sudo ./pmctl.py add-to-group social whatsapp

# 1 hour total for all social apps
sudo ./pmctl.py group-limit social 60
```

## File Locations

- **Configuration**: `/etc/process-monitor/config.json`
- **State/Usage Data**: `/var/lib/process-monitor/state.json`
- **Logs**: `/var/log/process-monitor/monitor.log`
- **Main Script**: `/usr/local/bin/process-monitor.py`
- **Service File**: `/etc/systemd/system/process-monitor.service`

## Security Notes

- The service runs as root to ensure it can monitor and control all processes
- Configuration files are only writable by root
- The service uses systemd security features to limit its capabilities
- Notifications are sent to user sessions when possible

## Troubleshooting

### Service Won't Start
```bash
# Check for errors
sudo journalctl -u process-monitor -n 50
# Verify Python and psutil installation
python3 -c "import psutil; print('OK')"
```

### Processes Not Being Blocked
- Verify the username is in `monitored_users`
- Check process names match (case-insensitive partial match)
- Ensure service is running: `sudo systemctl status process-monitor`

### Time Limits Not Working
- Check if usage is being tracked: `sudo ./pmctl.py usage`
- Verify process names in configuration
- Check logs for any errors

### Notifications Not Showing
- Desktop notifications require a display server
- May need to configure for specific desktop environments
- Check logs for notification errors

## Uninstallation

To completely remove the system:

```bash
# Stop and disable service
sudo systemctl stop process-monitor
sudo systemctl disable process-monitor

# Remove files
sudo rm /usr/local/bin/process-monitor.py
sudo rm /usr/local/bin/pmctl.py
sudo rm /etc/systemd/system/process-monitor.service
sudo rm -rf /etc/process-monitor
sudo rm -rf /var/lib/process-monitor
sudo rm -rf /var/log/process-monitor

# Reload systemd
sudo systemctl daemon-reload
```

## Tips for Parents

1. **Start Gradually**: Begin with monitoring only, then add limits as needed
2. **Be Transparent**: Discuss the rules with your child
3. **Set Reasonable Limits**: Consider homework and legitimate needs
4. **Review Usage**: Check logs weekly to understand patterns
5. **Adjust as Needed**: Modify limits based on behavior and age
6. **Use Warnings**: The 5-minute warning helps kids manage their time
7. **Weekend Differences**: Consider creating separate weekend configurations

## Advanced Usage

### Multiple Configurations
You can create different configurations for different situations:
```bash
# School days configuration
sudo cp /etc/process-monitor/config.json /etc/process-monitor/config-school.json

# Weekend configuration  
sudo cp /etc/process-monitor/config.json /etc/process-monitor/config-weekend.json

# Switch configurations
sudo cp /etc/process-monitor/config-weekend.json /etc/process-monitor/config.json
sudo systemctl reload process-monitor
```

### Scheduling Different Rules
Use cron to automatically switch configurations:
```bash
# Add to root's crontab
sudo crontab -e

# School days (Monday-Friday at 6 AM)
0 6 * * 1-5 cp /etc/process-monitor/config-school.json /etc/process-monitor/config.json && systemctl reload process-monitor

# Weekends (Saturday at 6 AM)
0 6 * * 6 cp /etc/process-monitor/config-weekend.json /etc/process-monitor/config.json && systemctl reload process-monitor
```

## Support

For issues or questions:
1. Check the logs first: `sudo journalctl -u process-monitor -n 100`
2. Verify configuration: `sudo ./pmctl.py config`
3. Test with verbose logging by modifying the service file

## License

This software is provided as-is for personal use. Feel free to modify for your needs.
