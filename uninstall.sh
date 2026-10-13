#!/bin/bash

# Child Minder Uninstall Script
# Run with sudo: sudo bash uninstall.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Child Minder Uninstall Script${NC}"
echo "==============================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
   echo -e "${RED}Please run as root (use sudo)${NC}"
   exit 1
fi

# Confirm uninstall
echo -e "${YELLOW}This will completely remove the Child Minder system.${NC}"
echo "Do you want to continue? (y/N)"
read -r response
if [[ ! "$response" =~ ^[Yy]$ ]]; then
    echo "Uninstall cancelled."
    exit 0
fi

# Ask about backup
echo ""
echo "Do you want to backup the configuration first? (Y/n)"
read -r backup_response
if [[ ! "$backup_response" =~ ^[Nn]$ ]]; then
    BACKUP_DIR="/var/backups/child-minder-$(date +%Y%m%d-%H%M%S)"
    echo "Creating backup in $BACKUP_DIR..."
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"

    # Each source tree is named child-minder, so they are kept in separate
    # subdirectories rather than being merged into one.
    if [ -d /etc/child-minder ]; then
        cp -r /etc/child-minder "$BACKUP_DIR/etc"
    fi

    if [ -d /var/lib/child-minder ]; then
        cp -r /var/lib/child-minder "$BACKUP_DIR/var-lib"
    fi

    if [ -d /var/log/child-minder ]; then
        cp -r /var/log/child-minder "$BACKUP_DIR/var-log"
    fi

    echo -e "${GREEN}Backup created at: $BACKUP_DIR${NC}"
    echo ""
fi

echo "Starting uninstallation..."

# Stop and disable service
echo "Stopping service..."
systemctl stop child-minder 2>/dev/null || true
systemctl disable child-minder 2>/dev/null || true

# Re-enable any disabled users
echo "Checking for disabled users..."
if [ -f /var/lib/child-minder/user_control.json ]; then
    echo "Re-enabling any disabled user accounts..."
    # A failed re-enable must stop the uninstall: the account would stay
    # expired while the files recording it are removed below.
    python3 -c "
import json
import os
import subprocess
import sys
path = '/var/lib/child-minder/user_control.json'
failures = []
if os.path.exists(path):
    with open(path, 'r') as f:
        data = json.load(f)
    for username in data.get('disabled_users', {}):
        print(f'Re-enabling user: {username}')
        # Clearing the expiry is what restores login; passwd -u is best effort
        # because it refuses to leave an account passwordless.
        result = subprocess.run(['usermod', '--expiredate', '', username],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f'Failed to re-enable {username}: {result.stderr.strip()}',
                  file=sys.stderr)
            failures.append(username)
        else:
            subprocess.run(['passwd', '-u', username], capture_output=True)
sys.exit(1 if failures else 0)
"
fi

# Remove service file
echo "Removing service file..."
rm -f /etc/systemd/system/child-minder.service
systemctl daemon-reload

# Remove executable files
echo "Removing executable files..."
rm -rf /usr/lib/child-minder
rm -f /usr/bin/child-minder.py
rm -f /usr/bin/cmctl.py
rm -f /usr/bin/cmctl

# Remove bash tab completion
rm -f /etc/bash_completion.d/cmctl

# Ask about removing data files
echo ""
echo -e "${YELLOW}Do you want to remove configuration and data files? (y/N)${NC}"
echo "This includes usage statistics and settings."
read -r remove_data
if [[ "$remove_data" =~ ^[Yy]$ ]]; then
    echo "Removing configuration and data files..."
    rm -rf /etc/child-minder
    rm -rf /var/lib/child-minder
    rm -rf /var/log/child-minder
else
    echo "Keeping configuration and data files."
    echo "Files remain at:"
    [ -d /etc/child-minder ] && echo "  - /etc/child-minder"
    [ -d /var/lib/child-minder ] && echo "  - /var/lib/child-minder"
    [ -d /var/log/child-minder ] && echo "  - /var/log/child-minder"
fi

echo ""
echo -e "${GREEN}Child Minder has been uninstalled.${NC}"

if [ -n "$BACKUP_DIR" ]; then
    echo ""
    echo "Your backup is saved at: $BACKUP_DIR"
    # Copying each directory's contents works whether or not the target
    # still exists; cp -r of the directory itself would nest it inside one.
    echo "To restore configuration, usage data and logs later:"
    echo "  sudo mkdir -p /etc/child-minder /var/lib/child-minder /var/log/child-minder"
    echo "  sudo cp -a $BACKUP_DIR/etc/. /etc/child-minder/"
    echo "  sudo cp -a $BACKUP_DIR/var-lib/. /var/lib/child-minder/"
    echo "  sudo cp -a $BACKUP_DIR/var-log/. /var/log/child-minder/"
fi

echo ""
echo "To reinstall, run: sudo bash install.sh"
