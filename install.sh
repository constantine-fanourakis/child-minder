#!/bin/bash

# Process Monitor Installation Script
# Run with sudo: sudo bash install.sh

set -e

echo "Process Monitor Installation Script"
echo "===================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
   echo "Please run as root (use sudo)"
   exit 1
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
pip3 install psutil || {
    echo "Failed to install with pip3, trying with apt..."
    apt-get update && apt-get install -y python3-psutil
}

# Install notification support
echo "Installing notification support..."
if command -v apt-get >/dev/null 2>&1; then
    # Debian/Ubuntu
    apt-get install -y libnotify-bin pulseaudio-utils dbus-x11 || true
elif command -v yum >/dev/null 2>&1; then
    # RHEL/CentOS/Fedora
    yum install -y libnotify pulseaudio-utils dbus-x11 || true
elif command -v pacman >/dev/null 2>&1; then
    # Arch Linux
    pacman -S --noconfirm libnotify pulseaudio dbus || true
fi

# Create directories
echo "Creating directories..."
mkdir -p /etc/process-monitor
mkdir -p /var/lib/process-monitor
mkdir -p /var/log/process-monitor
mkdir -p /usr/local/bin

# Check if all required files exist
REQUIRED_FILES=("process-monitor.py" "pmctl.py" "config.json" "process-monitor.service")
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "Error: Required file '$file' not found in current directory"
        echo "Please ensure all files are present before running the installer"
        exit 1
    fi
done

# Copy files
echo "Copying files..."
cp process-monitor.py /usr/local/bin/
chmod +x /usr/local/bin/process-monitor.py

cp pmctl.py /usr/local/bin/
chmod +x /usr/local/bin/pmctl.py

# Create convenience symlink for pmctl
ln -sf /usr/local/bin/pmctl.py /usr/local/bin/pmctl

# Check if config exists, don't overwrite if it does
if [ ! -f /etc/process-monitor/config.json ]; then
    cp config.json /etc/process-monitor/
    chmod 600 /etc/process-monitor/config.json
    echo "Configuration file created at /etc/process-monitor/config.json"
    echo "Please edit it to set your specific requirements:"
    echo "  - Update 'monitored_users' with the username(s) to monitor"
    echo "  - Adjust 'blocked_processes' list"
    echo "  - Set time limits in 'limited_processes' (in minutes)"
else
    echo "Configuration file already exists, skipping..."
fi

# Set proper permissions
chmod 755 /var/lib/process-monitor
chmod 750 /var/log/process-monitor

# Install systemd service
echo "Installing systemd service..."
cp process-monitor.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable service (but don't start it yet)
echo "Enabling service..."
systemctl enable process-monitor.service

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "1. Edit the configuration file: sudo nano /etc/process-monitor/config.json"
echo "   - Add your child's username to 'monitored_users'"
echo "   - Configure blocked processes and time limits"
echo "2. Start the service: sudo systemctl start process-monitor"
echo "3. Check service status: sudo systemctl status process-monitor"
echo "4. View logs: sudo journalctl -u process-monitor -f"
echo ""
echo "Useful commands:"
echo "  - Stop service: sudo systemctl stop process-monitor"
echo "  - Restart service: sudo systemctl restart process-monitor"
echo "  - Management utility: sudo pmctl --help"
echo "  - View detailed logs: sudo tail -f /var/log/process-monitor/monitor.log"
echo ""
echo "Testing recommendations:"
echo "  - Test with a test user account first"
echo "  - Start with monitoring only (no blocks or limits)"
echo "  - Gradually add restrictions after testing"
