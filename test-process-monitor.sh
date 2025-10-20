#!/bin/bash

# Process Monitor System Test Script
# Run after installation to verify everything works
# Run with: sudo bash test-process-monitor.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Process Monitor System Test Script${NC}"
echo "===================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
   echo -e "${RED}Please run as root (use sudo)${NC}"
   exit 1
fi

# Test function
test_feature() {
    local test_name=$1
    local command=$2
    echo -n "Testing $test_name... "
    if eval $command 2>/dev/null; then
        echo -e "${GREEN}✓${NC}"
        return 0
    else
        echo -e "${RED}✗${NC}"
        return 1
    fi
}

# Count passed/failed tests
PASSED=0
FAILED=0

echo ""
echo "Running system tests..."
echo ""

# Test 1: Check if Python 3.6+ is installed
if test_feature "Python 3.6+" 'python3 -c "import sys; exit(0 if sys.version_info >= (3,6) else 1)"'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 2: Check if psutil is installed
if test_feature "psutil module" 'python3 -c "import psutil"'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 3: Check if service file exists
if test_feature "service file" '[ -f /etc/systemd/system/process-monitor.service ]'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 4: Check if main script exists
if test_feature "main script" '[ -f /usr/local/bin/process-monitor.py ]'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 5: Check if management utility exists
if test_feature "management utility" '[ -f /usr/local/bin/pmctl.py ]'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 6: Check if config file exists
if test_feature "config file" '[ -f /etc/process-monitor/config.json ]'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 7: Check if directories exist
if test_feature "required directories" '[ -d /var/lib/process-monitor ] && [ -d /var/log/process-monitor ]'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 8: Check if notification system is available
if test_feature "notify-send" 'which notify-send'; then
    ((PASSED++))
else
    ((FAILED++))
    echo "  Warning: Desktop notifications may not work"
fi

# Test 9: Test pmctl command
if test_feature "pmctl command" 'pmctl config > /dev/null'; then
    ((PASSED++))
else
    ((FAILED++))
fi

# Test 10: Check if service can be loaded
if test_feature "service loading" 'systemctl daemon-reload && systemctl status process-monitor > /dev/null 2>&1 || [ $? -eq 3 ]'; then
    ((PASSED++))
else
    ((FAILED++))
fi

echo ""
echo "===================================="
echo -e "Test Results: ${GREEN}$PASSED passed${NC}, ${RED}$FAILED failed${NC}"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All tests passed! The system is ready to use.${NC}"
    echo ""
    echo "Next steps:"
    echo "1. Edit config: sudo nano /etc/process-monitor/config.json"
    echo "2. Add a test user to 'monitored_users'"
    echo "3. Start service: sudo systemctl start process-monitor"
    echo "4. Check status: sudo systemctl status process-monitor"
else
    echo -e "${YELLOW}Some tests failed. Please check the installation.${NC}"
    echo ""
    echo "Common fixes:"
    echo "- Install psutil: pip3 install psutil"
    echo "- Install notify-send: apt install libnotify-bin"
    echo "- Re-run installer: sudo bash install.sh"
fi

echo ""
echo "Optional: Test warning system (requires display):"
echo "  DISPLAY=:0 notify-send 'Test' 'If you see this, notifications work!'"
echo ""

# Test configuration validity
echo "Validating configuration file..."
python3 -c "
import json
import sys
try:
    with open('/etc/process-monitor/config.json', 'r') as f:
        config = json.load(f)
    print('Configuration is valid JSON')
    if not config.get('monitored_users'):
        print('Warning: No users configured for monitoring')
        print('Add usernames to monitored_users in config.json')
    else:
        print(f\"Monitoring users: {', '.join(config['monitored_users'])}\")
    if config.get('blocked_processes'):
        print(f\"Blocked processes: {', '.join(config['blocked_processes'])}\")
    if config.get('limited_processes'):
        print(f\"Limited processes: {', '.join(config['limited_processes'].keys())}\")
except Exception as e:
    print(f'Error in configuration: {e}')
    sys.exit(1)
" || echo -e "${RED}Configuration validation failed${NC}"