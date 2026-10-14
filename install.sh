#!/bin/bash

# Child Minder Installation/Update Script
# Run with sudo: sudo bash install.sh
#
# This script handles both fresh installations and updates.
# On update, it preserves your existing configuration.

set -e

echo "Child Minder Installation Script"
echo "================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
   echo "Please run as root (use sudo)"
   exit 1
fi

# Detect if this is an update
IS_UPDATE=false
if { [ -f /usr/lib/child-minder/child-minder.py ] || [ -f /usr/bin/child-minder.py ]; } \
        && [ -f /etc/child-minder/config.json ]; then
    IS_UPDATE=true
    echo "Existing installation detected - performing update"
    echo ""
fi

# The service runs /usr/bin/python3 (see child-minder.service), so every
# interpreter check and build step here must use that same binary, not
# whatever python3 happens to be first on root's PATH.
PYTHON=/usr/bin/python3

# Check Python version
# 3.9 is the floor: dropping privileges for notification helpers uses the
# user/group arguments to subprocess, added in that release.
PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
REQUIRED_VERSION="3.9"
if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "Error: Python 3.9 or higher is required. Found: $PYTHON_VERSION"
    exit 1
fi

# Check for systemd
if ! command -v systemctl &> /dev/null; then
    echo "Error: This system requires systemd"
    exit 1
fi

# Install required Python packages
# The distribution package is preferred: installing psutil with pip as root
# shadows the packaged copy, and fails outright on PEP 668 systems.
echo "Installing required Python packages..."
if "$PYTHON" -c "import psutil" 2>/dev/null; then
    echo "psutil already available."
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3-psutil
elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install python3-psutil
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-psutil
elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-psutil
elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm python-psutil
else
    echo "Error: Could not install psutil with a system package manager."
    echo "Please install it manually, for example: pip3 install psutil"
    exit 1
fi

if ! "$PYTHON" -c "import psutil" 2>/dev/null; then
    echo "Error: psutil is still not importable after installation"
    exit 1
fi

# Install notification support
echo "Installing notification support..."
if command -v apt-get >/dev/null 2>&1; then
    # Debian/Ubuntu
    apt-get install -y libnotify-bin pulseaudio-utils 2>/dev/null || true
elif command -v zypper >/dev/null 2>&1; then
    # openSUSE/SUSE
    zypper --non-interactive install libnotify-tools pulseaudio-utils 2>/dev/null || true
elif command -v yum >/dev/null 2>&1; then
    # RHEL/CentOS/Fedora
    yum install -y libnotify pulseaudio-utils 2>/dev/null || true
elif command -v pacman >/dev/null 2>&1; then
    # Arch Linux
    pacman -S --noconfirm libnotify pulseaudio dbus 2>/dev/null || true
fi

# Create directories
echo "Creating directories..."
# The web user must traverse the config directory to read its 0640 web.json,
# even when the administrator installs with a restrictive umask.
install -d -m 755 /etc/child-minder
install -d -m 700 /var/lib/child-minder /var/log/child-minder

# Check if all required files exist
REQUIRED_FILES=("child-minder.py" "cmctl.py" "child-minder-web.py" "child-minder-broker.py"
    "childminder/__init__.py" "childminder/accounts.py" "childminder/broker.py"
    "childminder/cli.py" "childminder/config.py" "childminder/daemon.py"
    "childminder/policy.py" "childminder/schedule.py" "childminder/store.py"
    "childminder/web.py" "childminder/webauth.py" "childminder/x11.py"
    "childminder/static/index.html" "childminder/static/app.js" "childminder/static/style.css"
    "config.json" "child-minder.service" "child-minder-web.service"
    "child-minder-broker.socket" "child-minder-broker@.service" "cmctl-completion.bash")
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "Error: Required file '$file' not found in current directory"
        echo "Please ensure all files are present before running the installer"
        exit 1
    fi
done

# Stop service if running (for update)
if $IS_UPDATE && systemctl is-active --quiet child-minder 2>/dev/null; then
    echo "Stopping child-minder service for update..."
    systemctl stop child-minder
fi

# Copy files
echo "Copying files..."
# The entry scripts and the childminder package they import live together;
# Python resolves the /usr/bin symlinks and finds the package beside them.
LIB_DIR=/usr/lib/child-minder
rm -rf "$LIB_DIR"
install -d -m 755 "$LIB_DIR" "$LIB_DIR/childminder"
install -m 755 child-minder.py cmctl.py child-minder-web.py child-minder-broker.py "$LIB_DIR/"
install -m 644 childminder/*.py "$LIB_DIR/childminder/"
install -d -m 755 "$LIB_DIR/childminder/static"
install -m 644 childminder/static/* "$LIB_DIR/childminder/static/"
# The services cannot write bytecode under read-only /usr. The web user must
# read the precompiled files as well as the source, whatever the installer's umask.
(umask 022; "$PYTHON" -m compileall -q "$LIB_DIR")

ln -sf "$LIB_DIR/cmctl.py" /usr/bin/cmctl
ln -sf "$LIB_DIR/cmctl.py" /usr/bin/cmctl.py
# The daemon runs from $LIB_DIR; remove a copy from the pre-package layout
rm -f /usr/bin/child-minder.py

# Install bash tab completion
if [ -d /etc/bash_completion.d ]; then
    cp cmctl-completion.bash /etc/bash_completion.d/cmctl
    echo "Bash tab completion installed."
fi

# Handle configuration file
if [ ! -f /etc/child-minder/config.json ]; then
    # Fresh install - copy default config
    cp config.json /etc/child-minder/
    chmod 600 /etc/child-minder/config.json
    echo "Configuration file created at /etc/child-minder/config.json"
    echo "Please edit it to set your specific requirements:"
    echo "  - Update 'monitored_users' with the username(s) to monitor"
    echo "  - Adjust 'blocked_processes' list"
    echo "  - Set time limits in 'limited_processes' (in minutes)"
else
    # Update - preserve existing config, but save new default as reference
    cp config.json /etc/child-minder/config.json.new
    chmod 600 /etc/child-minder/config.json.new
    echo "Existing configuration preserved."
    echo "New default config saved as /etc/child-minder/config.json.new for reference."

    # config.upgrade() rewrites what older releases meant by a setting, once:
    # an empty monitored_users used to mean every regular account. The
    # last-good copy is upgraded too, since the daemon may start from it.
    PYTHONPATH="$LIB_DIR" "$PYTHON" - /etc/child-minder/config.json \
            /var/lib/child-minder/config.last-good.json <<'EOF' || true
import json, sys
from childminder import config
from childminder.store import atomic_write_json
for path in sys.argv[1:]:
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        continue
    if not isinstance(raw, dict):
        continue
    was_empty = raw.get("monitored_users", []) == []
    if config.upgrade(raw):
        atomic_write_json(path, raw)
        if was_empty and path.endswith("/config.json"):
            print("")
            print("monitored_users was empty, which used to mean every regular user.")
            print('It now means nobody, so it was set to ["*"], which keeps monitoring')
            print("every regular user. Replace * with names to monitor only them.")
EOF

    # A pattern without a glob character matches only a process name equal to
    # the whole pattern, so list those in case one was meant as a prefix.
    "$PYTHON" - /etc/child-minder/config.json <<'EOF' || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        config = json.load(f)
    lists = [config.get("blocked_processes", []), config.get("monitored_processes", []),
             list(config.get("limited_processes", {}))]
    lists += list(config.get("process_groups", {}).values())
except (OSError, ValueError, AttributeError, TypeError):
    sys.exit(0)
exact = sorted({p for patterns in lists for p in patterns
                if isinstance(p, str) and not any(c in p for c in "*?[")})
if exact:
    print("")
    print("Note: these patterns match a process name exactly, not as a substring:")
    print("  " + ", ".join(exact))
    print("Add a trailing * to any that should also match helper processes,")
    print("for example steam* to catch steamwebhelper.")
EOF
fi

# The web UI front end runs as this unprivileged system user. Its group may
# connect to the broker socket and read web.json, nothing else of ours.
WEB_USER=child-minder-web
if ! getent passwd "$WEB_USER" >/dev/null; then
    echo "Creating system user $WEB_USER for the web UI..."
    useradd --system --user-group --no-create-home --home-dir / \
        --shell /usr/sbin/nologin "$WEB_USER"
fi
if [ -f /etc/child-minder/web.json ]; then
    chgrp "$WEB_USER" /etc/child-minder/web.json
    chmod 640 /etc/child-minder/web.json
fi

# Usage data records what the monitored user has been running, so neither
# directory is readable by anyone but root.
chmod 700 /var/lib/child-minder
chmod 700 /var/log/child-minder
chmod 600 /var/lib/child-minder/* 2>/dev/null || true

# Install systemd units
echo "Installing systemd units..."
cp child-minder.service child-minder-web.service child-minder-broker.socket \
    child-minder-broker@.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable service
echo "Enabling service..."
systemctl enable child-minder.service

# restart also starts a stopped service. A fresh install's default config
# monitors only a placeholder user, so the running service does nothing until
# it is configured.
echo "Starting child-minder service..."
systemctl restart child-minder

echo "Starting the web UI..."
systemctl enable child-minder-broker.socket child-minder-web.service
systemctl restart child-minder-broker.socket
systemctl restart child-minder-web.service

WEB_PORT=$("$PYTHON" - <<'EOF' 2>/dev/null || echo 8765
import json
try:
    port = json.load(open("/etc/child-minder/web.json")).get("port", 8765)
except (OSError, ValueError):
    port = 8765
print(port if isinstance(port, int) and not isinstance(port, bool) else 8765)
EOF
)
echo ""
echo "Web UI: http://<this computer's address>:${WEB_PORT}/"
if ! "$PYTHON" -c 'import json, sys; sys.exit("password" not in json.load(open("/etc/child-minder/web.json")))' 2>/dev/null; then
    echo "  Set its password first: sudo cmctl web-password"
fi
# The installer does not open the port: a laptop's connections all land in
# firewalld's default zone unless assigned, so a rule there would also expose
# the UI on other networks. Only the administrator knows which one is home.
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    echo "  firewalld is running. To reach the web UI from other devices on your"
    echo "  home network only, put that connection in the home zone and open the"
    echo "  port there:"
    echo "    sudo nmcli connection modify \"<home network name>\" connection.zone home"
    echo "    sudo nmcli connection up \"<home network name>\""
    echo "    sudo firewall-cmd --permanent --zone=home --add-port=${WEB_PORT}/tcp"
    echo "    sudo firewall-cmd --reload"
else
    echo "  If a firewall is active, allow TCP port ${WEB_PORT} from your home network."
fi

if $IS_UPDATE; then
    echo ""
    echo "Update complete!"
    echo ""
    echo "The service has been restarted with the new version."
    echo "Your configuration has been preserved."
    echo ""
    echo "If there are new configuration options, check:"
    echo "  /etc/child-minder/config.json.new"
    echo ""
    echo "Check service status: sudo systemctl status child-minder"
    echo "View logs: sudo journalctl -u child-minder -f"
else
    # Fresh install
    echo ""
    echo "Installation complete! The service is running."
    echo ""
    echo "Next steps:"
    echo "1. Add your child's username: sudo cmctl add-user USERNAME"
    echo "   and remove the placeholder: sudo cmctl remove-user son_username"
    echo "2. Configure blocked processes and time limits with cmctl, or edit"
    echo "   /etc/child-minder/config.json and run: sudo systemctl reload child-minder"
    echo "3. Check service status: sudo systemctl status child-minder"
    echo "4. View logs: sudo journalctl -u child-minder -f"
    echo ""
    echo "Useful commands:"
    echo "  - Stop service: sudo systemctl stop child-minder"
    echo "  - Restart service: sudo systemctl restart child-minder"
    echo "  - Management utility: sudo cmctl --help"
    echo "  - View detailed logs: sudo tail -f /var/log/child-minder/minder.log"
    echo ""
    echo "Testing recommendations:"
    echo "  - Test with a test user account first"
    echo "  - Start with monitoring only (no blocks or limits)"
    echo "  - Gradually add restrictions after testing"
fi
