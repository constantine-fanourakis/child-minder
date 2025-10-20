#!/bin/bash

# Process Monitor Uninstall Script
# Run with sudo: sudo bash uninstall.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Process Monitor Uninstall Script${NC}"
echo "===================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
   echo -e "${RED}Please run as root (use sudo)${NC}"
   exit 1
fi

# Confirm uninstall
echo -e "${YELLOW}This will completely remove the Process Monitor system.${NC}"
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
    BACKUP_DIR="/tmp/process-monitor-backup-$(date +%Y%m%d-%H%M%S)"
    echo "Creating backup in $BACKUP_DIR..."
    mkdir -p "$BACKUP_DIR"
    
    # Backup configuration
    if [ -d /etc/process-monitor ]; then
        cp -r /etc/process-monitor "$BACKUP_DIR/"
    fi
    
    # Backup state files
    if [ -d /var/lib/process-monitor ]; then
        cp -r /var/lib/process-monitor "$BACKUP_DIR/"
    fi
    
    # Backup logs
    if [ -d /var/log/process-monitor ]; then
        cp -r /var/log/process-monitor "$BACKUP_DIR/"
    fi
    
    echo -e "${GREEN}Backup created at: $BACKUP_DIR${NC}"
    echo ""
fi

echo "Starting uninstallation..."

# Stop and disable service
echo "Stopping service..."
systemctl stop process-monitor 2>/dev/null || true
systemctl disable process-monitor 2>/dev/null || true

# Re-enable any disabled users
echo "Checking for disabled users..."
if [ -f /var/lib/process-monitor/user_control.json ]; then
    echo "Re-enabling any disabled user accounts..."
    python3 -c "
import json
import subprocess
import os
if os.path.exists('/var/lib/process-monitor/user_control.json'):
    with open('/var/lib/process-monitor/user_control.json', 'r') as f:
        data = json.load(f)
        for username in data.get('disabled_users', {}).keys():
            print(f'Re-enabling user: {username}')
            subprocess.run(['passwd', '-u', username], capture_output=True)
" 2>/dev/null || true
fi

# Remove service file
echo "Removing service file..."
rm -f /etc/systemd/system/process-monitor.service
systemctl daemon-reload

# Remove executable files
echo "Removing executable files..."
rm -f /usr/local/bin/process-monitor.py
rm -f /usr/local/bin/pmctl.py
rm -f /usr/local/bin/pmctl

# Ask about removing data files
echo ""
echo -e "${YELLOW}Do you want to remove configuration and data files? (y/N)${NC}"
echo "This includes usage statistics and settings."
read -r remove_data
if [[ "$remove_data" =~ ^[Yy]$ ]]; then
    echo "Removing configuration and data files..."
    rm -rf /etc/process-monitor
    rm -rf /var/lib/process-monitor
    rm -rf /var/log/process-monitor
else
    echo "Keeping configuration and data files."
    echo "Files remain at:"
    [ -d /etc/process-monitor ] && echo "  - /etc/process-monitor"
    [ -d /var/lib/process-monitor ] && echo "  - /var/lib/process-monitor"
    [ -d /var/log/process-monitor ] && echo "  - /var/log/process-monitor"
fi

echo ""
echo -e "${GREEN}Process Monitor has been uninstalled.${NC}"

if [ -n "$BACKUP_DIR" ]; then
    echo ""
    echo "Your backup is saved at: $BACKUP_DIR"
    echo "To restore configuration later:"
    echo "  sudo cp -r $BACKUP_DIR/process-monitor /etc/"
    echo "  sudo cp -r $BACKUP_DIR/process-monitor /var/lib/"
fi

echo ""
echo "To reinstall, run: sudo bash install.sh"