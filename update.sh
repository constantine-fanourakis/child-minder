#!/bin/bash

# Process Monitor Update Script
# Run with: sudo bash update.sh

set -e

# Configure your repository URL here
REPO_URL="https://github.com/constantine-fanourakis/child-minder.git"
TEMP_DIR=$(mktemp -d)

echo "Process Monitor Update Script"
echo "=============================="

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo "Error: git is not installed. Please install git first."
    exit 1
fi

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

echo "Downloading latest version..."
git clone --depth 1 "$REPO_URL" "$TEMP_DIR"

# Run the existing installer
cd "$TEMP_DIR"
echo ""
bash install.sh

# Restart service if running
if systemctl is-active --quiet process-monitor; then
    echo "Restarting service..."
    systemctl restart process-monitor
    echo "Service restarted."
fi

# Cleanup
rm -rf "$TEMP_DIR"

echo ""
echo "Update complete!"
