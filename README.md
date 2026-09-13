# Child Minder

A parental control system for Linux that monitors and controls application usage. Block applications, set daily time limits (per-app or per-group), enforce access schedules with separate weekday/weekend hours, and disable user accounts on demand.

## Features

- Block specified applications from running
- Set daily time limits for individual applications
- Set combined time limits for groups of applications (e.g., all games = 2 hours total)
- Weekday and weekend access schedules with multiple time windows
- Overnight schedule support (e.g., `22:00-06:00`)
- Disable/enable user accounts with optional timed re-enable
- Desktop notifications warning users before limits expire
- Usage tracking and logging
- Runs as a systemd service with automatic startup
- SIGHUP config reload (no restart required)

## Requirements

- Linux system with systemd
- Python 3.9+
- Root/sudo access
- Python package: `psutil`

## Installation

### Option 1: Git clone

```bash
git clone <repo-url> /tmp/child-minder
cd /tmp/child-minder
sudo bash install.sh
```

### Option 2: Download files

Download all files to a directory, then:

```bash
cd /path/to/child-minder
sudo bash install.sh
```

The installer handles both fresh installs and updates (preserving your existing config). Tab completion for `cmctl` is installed automatically; open a new shell or run `source /etc/bash_completion.d/cmctl` to activate it. It installs:

- `/usr/bin/child-minder.py` — main daemon
- `/usr/bin/cmctl.py` — management utility
- `/usr/bin/cmctl` — convenience symlink for `cmctl.py`
- `/etc/child-minder/config.json` — configuration
- `/etc/systemd/system/child-minder.service` — systemd unit

## Quick Start

```bash
# 1. Add your child's username
sudo cmctl add-user johnny

# 2. Block unwanted apps
sudo cmctl block discord

# 3. Set time limits
sudo cmctl limit minecraft 60          # 60 min/day
sudo cmctl add-to-group games steam
sudo cmctl add-to-group games roblox
sudo cmctl group-limit games 120       # 2 hours total for all games

# 4. Set access hours
sudo cmctl set-weekday-hours johnny 15 21   # 3 PM - 9 PM on weekdays
sudo cmctl set-weekend-hours johnny 9 22    # 9 AM - 10 PM on weekends

# 5. Start the service
sudo systemctl start child-minder
```

## Configuration Reference

Configuration lives at `/etc/child-minder/config.json`. Changes are picked up on service reload (`systemctl reload child-minder` or `kill -HUP <pid>`).

### Config keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Master on/off switch |
| `check_interval` | int | `5` | Seconds between process checks |
| `min_uid` | int | `1000` | Lowest UID that may be monitored; system accounts are never touched |
| `monitored_users` | list | `[]` | Usernames to monitor (empty = all users at or above `min_uid`) |
| `blocked_processes` | list | `[]` | Process patterns to block completely |
| `limited_processes` | dict | `{}` | Process pattern → daily limit in minutes |
| `process_groups` | dict | `{}` | Group name → list of process patterns |
| `group_limits` | dict | `{}` | Group name → daily limit in minutes |
| `user_daily_limits` | dict | `{}` | Username → overall daily screen time, in minutes (see below) |
| `monitored_processes` | list | `[]` | Process patterns to track usage for (logging only) |
| `warning_time` | int | `300` | Seconds before limit to warn (single warning) |
| `warning_intervals` | list | — | Multiple warning times in seconds (overrides `warning_time`) |
| `usage_log_interval` | int | `60` | Seconds between usage summary logs |
| `user_control` | dict | see below | User account control settings |

### `user_control` sub-keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable user account control features |
| `check_interval` | int | `60` | Seconds between account and schedule checks |

### `user_daily_limits`

Either a plain number of minutes, or separate weekday and weekend allowances:

```json
"user_daily_limits": {
  "johnny": {"weekday": 120, "weekend": 240}
}
```

Screen time accrues while the user runs anything listed in `limited_processes`,
`monitored_processes`, or a process group. When the allowance runs out the
user's session is closed.

### Process patterns

A pattern matches a process name exactly unless it contains `*`, `?` or `[`, in
which case it is treated as a shell glob. Matching is case-insensitive.

```json
"process_groups": {
  "games": ["minecraft*", "steam*"]
}
```

Use a trailing `*` for applications that spawn differently named helper
processes: `steam*` also covers `steamwebhelper`, and `chrome*` covers
`chrome_crashpad_handler`. All processes matching one pattern share a single
counter, so a browser running thirty processes still accrues time once.

Patterns are never matched as substrings. An entry of `sh` matches only a
process actually called `sh`, not `bash` or `zsh`.

### Example config

```json
{
  "enabled": true,
  "check_interval": 5,
  "monitored_users": ["johnny"],
  "blocked_processes": ["discord*", "telegram*"],
  "process_groups": {
    "games": ["minecraft*", "steam*", "roblox*"],
    "browsers": ["firefox*", "chrome*", "chromium*"]
  },
  "group_limits": {
    "games": 120,
    "browsers": 180
  },
  "limited_processes": {
    "minecraft*": 60,
    "youtube*": 45
  },
  "user_daily_limits": {
    "johnny": {"weekday": 120, "weekend": 240}
  },
  "monitored_processes": ["firefox*", "minecraft*"],
  "warning_intervals": [1800, 900, 600, 300, 120, 60],
  "usage_log_interval": 300,
  "user_control": {
    "enabled": true,
    "check_interval": 60
  }
}
```

### Group limits vs individual limits

When both are set, **both are enforced** — whichever is reached first triggers termination. Example: if the "games" group has 120 minutes and "minecraft" has 60 minutes individually, Minecraft is capped at 60 minutes but all games combined are capped at 120.

## Management Utility (`cmctl`)

Commands that change configuration or account state require `sudo`; the
read-only commands (`config`, `usage`, `status`, `logs`, `groups`,
`user-status`) do not. You can use either `cmctl` or `cmctl.py`.

### View information

```bash
cmctl config                # Show current configuration
cmctl usage                 # Show today's usage statistics
cmctl status                # Show service status
cmctl groups                # List process groups and limits
cmctl logs [-n 100]         # Show recent log entries
cmctl user-status [USER]    # Show user account status (all if no user given)
```

### Block/unblock processes

```bash
cmctl block discord         # Block Discord
cmctl unblock discord       # Remove block
```

### Individual time limits

```bash
cmctl limit minecraft 60    # 60 min/day
cmctl unlimit minecraft     # Remove limit
```

### Process groups

```bash
cmctl add-to-group games minecraft      # Add to group
cmctl remove-from-group games minecraft # Remove from group
cmctl group-limit games 120             # Set group limit (minutes)
cmctl group-unlimit games               # Remove group limit
```

### User management

```bash
cmctl add-user johnny       # Monitor this user
cmctl remove-user johnny    # Stop monitoring
```

### User account control

```bash
cmctl disable-user johnny -r "Reason"       # Disable account (immediate logout)
cmctl disable-user johnny -t 2 -r "Timeout" # Disable for 2 hours (auto re-enable)
cmctl enable-user johnny                    # Re-enable account
```

### Access schedules

```bash
# Set hours (replaces all windows for that day type)
cmctl set-weekday-hours johnny 15 21        # Mon-Fri: 3 PM - 9 PM
cmctl set-weekend-hours johnny 9 22         # Sat-Sun: 9 AM - 10 PM
cmctl set-user-hours johnny 8 21            # Alias for set-weekday-hours

# Multiple time windows
cmctl add-weekday-window johnny 7 8         # Add morning window (7-8 AM)
cmctl add-weekend-window johnny 12 14       # Add afternoon window
cmctl remove-weekday-window johnny 7 8      # Remove a window
cmctl remove-weekend-window johnny 12 14    # Remove a window
```

Overnight schedules are supported — use a start hour greater than the end hour (e.g., `22 6` for 10 PM to 6 AM).

### System control

```bash
cmctl enable                # Enable monitoring
cmctl disable               # Disable monitoring (temporary)
cmctl reset                 # Reset daily usage counters (signals the running service)
```

## Access Schedules

Access schedules control when a user can log in. Outside allowed hours, the user is automatically logged out and all their processes are terminated.

A day type with no windows is unrestricted. Weekdays and weekends are set
independently, so restricting one leaves the other open until you restrict it
too, and removing a user's last window for a day type returns that day to
unrestricted rather than locking them out.

### Separate weekday/weekend hours

```bash
# Restrict school days only; weekends stay unrestricted
sudo cmctl set-weekday-hours johnny 15 20   # 3 PM - 8 PM

# Restrict weekends as well
sudo cmctl set-weekend-hours johnny 9 22    # 9 AM - 10 PM

# Lift the weekday restriction again
sudo cmctl clear-weekday-windows johnny
```

### Clearing a schedule

```bash
sudo cmctl clear-weekday-windows johnny   # weekdays unrestricted
sudo cmctl clear-weekend-windows johnny   # weekends unrestricted
sudo cmctl clear-user-hours johnny        # unrestricted at any time
```

`remove-weekday-window` and `remove-weekend-window` drop one window at a time
and need its exact hours; the clear commands remove all of them at once.

### Multiple time windows

You can define multiple access windows per day. For example, allow morning and afternoon access with a break:

```bash
# Weekday: 7-8 AM (before school) and 3-9 PM (after school)
sudo cmctl set-weekday-hours johnny 15 21   # Main window
sudo cmctl add-weekday-window johnny 7 8    # Additional morning window
```

### Overnight schedules

For schedules that cross midnight, set the start hour greater than the end hour:

```bash
# Allow 10 PM to 6 AM (overnight)
sudo cmctl set-weekend-hours johnny 22 6
```

## User Account Control

Disable and re-enable user accounts for immediate enforcement. When disabled:

- User is immediately logged out
- The account is expired and the password locked, blocking every login path
  including SSH keys and passwordless display manager sessions
- All running processes are terminated

```bash
# Disable with reason
sudo cmctl disable-user johnny -r "Homework not done"

# Disable for a set duration (auto re-enables)
sudo cmctl disable-user johnny -t 2 -r "2-hour timeout"

# Manually re-enable
sudo cmctl enable-user johnny

# Check status
sudo cmctl user-status johnny
```

User control must be enabled in config (`user_control.enabled: true`). It is automatically enabled when you set access hours via `cmctl`.

## Service Management

```bash
sudo systemctl start child-minder      # Start
sudo systemctl stop child-minder       # Stop
sudo systemctl restart child-minder    # Restart
sudo systemctl reload child-minder     # Reload config (sends SIGHUP)
sudo systemctl status child-minder     # Check status
sudo systemctl enable child-minder     # Enable auto-start at boot
```

### Logs

```bash
sudo journalctl -u child-minder -f             # Live system logs
sudo journalctl -u child-minder --since today   # Today's logs
sudo tail -f /var/log/child-minder/minder.log   # Application log
```

## File Locations

| Path | Description |
|------|-------------|
| `/etc/child-minder/config.json` | Configuration |
| `/var/lib/child-minder/state.json` | Usage/state data |
| `/var/lib/child-minder/user_control.json` | User control state |
| `/var/log/child-minder/minder.log` | Application log |
| `/usr/bin/child-minder.py` | Main daemon |
| `/usr/bin/cmctl.py` | Management utility |
| `/usr/bin/cmctl` | Symlink to `cmctl.py` |
| `/etc/systemd/system/child-minder.service` | Systemd unit |

## Troubleshooting

### Service won't start

```bash
sudo journalctl -u child-minder -n 50
python3 -c "import psutil; print('OK')"
```

### Processes not being blocked

- Verify the username is in `monitored_users` and their UID is at or above `min_uid`
- Check the pattern matches the process name exactly, or add a trailing `*`.
  Confirm the name with `ps -eo comm= | sort -u | grep -i <app>`
- Ensure service is running: `sudo systemctl status child-minder`

### Time limits not working

- Check usage: `sudo cmctl usage`
- Verify the patterns in config match the real process names
- Check logs for errors

### Configuration changes have no effect

A configuration file that does not parse is rejected and the service keeps
running on the previous one, logging the error. Check with:

```bash
python3 -m json.tool /etc/child-minder/config.json
sudo journalctl -u child-minder -n 20
```

The last configuration that parsed is kept at
`/var/lib/child-minder/config.last-good.json` and is loaded automatically if the
live file is unreadable at startup.

### Notifications not showing

- Desktop notifications require `libnotify` and a running notification daemon
- Install: `sudo apt install libnotify-bin` (Debian/Ubuntu)

### Testing

A test script is included for validating the setup:

```bash
sudo bash test-child-minder.sh
```

## Uninstallation

```bash
sudo bash uninstall.sh
```

The script stops the service, re-enables any disabled user accounts, removes installed files, and optionally backs up your configuration.

## Limitations

Enforcement matches on process names, so it restrains a child who plays by the
rules rather than one actively working around it. Copying or renaming a binary
is enough to escape every limit here, and nothing detects that. Containing a
determined user needs cgroup or LSM level controls, which this tool does not
attempt.

Usage is sampled every `check_interval` seconds, so recorded time can be off by
up to one interval per application per day.

## See Also

- [QUICK-REFERENCE.md](QUICK-REFERENCE.md) — condensed command cheat sheet

## License

MIT — see [LICENSE](LICENSE) for details.
