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
if [ -f /usr/bin/child-minder.py ] && [ -f /etc/child-minder/config.json ]; then
    IS_UPDATE=true
    echo "Existing installation detected - performing update"
    echo ""
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
REQUIRED_VERSION="3.6"
if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "Error: Python 3.6 or higher is required. Found: $PYTHON_VERSION"
    exit 1
fi

# Check for systemd
if ! command -v systemctl &> /dev/null; then
    echo "Error: This system requires systemd"
    exit 1
fi

# Install required Python packages
echo "Installing required Python packages..."
pip3 install psutil 2>/dev/null || {
    echo "Failed to install with pip3, trying with system package manager..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y python3-psutil
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3-psutil
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3-psutil
    elif command -v pacman >/dev/null 2>&1; then
        pacman -S --noconfirm python3-psutil
    else
        echo "Error: Could not install psutil. Please install manually with: pip3 install psutil"
        exit 1
    fi
}

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
mkdir -p /etc/child-minder
mkdir -p /var/lib/child-minder
mkdir -p /var/log/child-minder

# Check if all required files exist
REQUIRED_FILES=("child-minder.py" "cmctl.py" "config.json" "child-minder.service")
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
cp child-minder.py /usr/bin/
chmod +x /usr/bin/child-minder.py

cp cmctl.py /usr/bin/
chmod +x /usr/bin/cmctl.py

# Create convenience symlink for cmctl
ln -sf /usr/bin/cmctl.py /usr/bin/cmctl

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
fi

# Set proper permissions
chmod 755 /var/lib/child-minder
chmod 750 /var/log/child-minder

# Install systemd service
echo "Installing systemd service..."
cp child-minder.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable service
echo "Enabling service..."
systemctl enable child-minder.service

if $IS_UPDATE; then
    # Update complete - restart service
    echo "Starting child-minder service..."
    systemctl start child-minder

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
    echo "Installation complete!"
    echo ""
    echo "Next steps:"
    echo "1. Edit the configuration file: sudo nano /etc/child-minder/config.json"
    echo "   - Add your child's username to 'monitored_users'"
    echo "   - Configure blocked processes and time limits"
    echo "2. Start the service: sudo systemctl start child-minder"
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
