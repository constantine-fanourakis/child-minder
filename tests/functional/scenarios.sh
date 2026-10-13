#!/bin/bash

# Functional scenarios, run by run.sh inside the test container as root.
# /root/new holds the checkout; with FROM_REV set, /root/old holds that
# revision, which is installed first so the upgrade path is exercised.

set -u

LOG=/var/log/child-minder/minder.log
CONFIG=/etc/child-minder/config.json
PASSED=0
FAILURES=()

ok() { echo "PASS  $1"; PASSED=$((PASSED + 1)); }
ko() { echo "FAIL  $1"; FAILURES+=("$1"); }

# check NAME COMMAND...: record whether COMMAND succeeds
check() {
    local name=$1; shift
    if "$@" >/dev/null 2>&1; then ok "$name"; else ko "$name"; fi
}

# wait_for SECONDS COMMAND...: succeed as soon as COMMAND does
wait_for() {
    local end=$((SECONDS + $1)); shift
    until "$@" >/dev/null 2>&1; do
        [ $SECONDS -ge $end ] && return 1
        sleep 1
    done
}

# Log lines written since the last mark, so each scenario sees only its own
mark() { LOG_MARK=$(wc -l < "$LOG"); }
logged() { tail -n +$((LOG_MARK + 1)) "$LOG" | grep -q -- "$1"; }

service_active() { systemctl is-active --quiet child-minder; }
kid_has_processes() { pgrep -u kid >/dev/null; }
kid_has_no_processes() { ! pgrep -u kid >/dev/null; }
kid_runs() { pgrep -u kid -x "$1" >/dev/null; }
kid_stopped() { ! pgrep -u kid -x "$1" >/dev/null; }
account_open() { chage -l kid | grep -q '^Account expires.*never'; }
account_expired() { ! account_open; }
password_unlocked() { [ "$(passwd -S kid | cut -d' ' -f2)" = P ]; }

# start_as_kid NAME: run a renamed sleep as kid, detached from this shell
start_as_kid() {
    [ -x "/usr/local/bin/$1" ] || cp /bin/sleep "/usr/local/bin/$1"
    su kid -s /bin/bash -c "setsid $1 1000 >/dev/null 2>&1 &"
    wait_for 5 kid_runs "$1"
}

reload_and_wait() {
    mark
    systemctl reload child-minder
    wait_for 15 logged "Configuration reloaded successfully"
}

# Clear usage and the session-end throttle between scenarios
reset_usage() {
    mark
    cmctl reset >/dev/null
    wait_for 15 logged "Resetting usage counters"
}

end_kid_processes() {
    pkill -KILL -u kid 2>/dev/null
    wait_for 5 kid_has_no_processes
}

today_type() { [ "$(date +%u)" -le 5 ] && echo weekday || echo weekend; }

WEB=http://127.0.0.1:8765
CSRF=""
# web METHOD PATH [JSON]: send a web UI request with the session cookie,
# leaving the body in /tmp/web.out and printing the HTTP status
web() {
    curl -s -o /tmp/web.out -w '%{http_code}' -b /tmp/web.jar -c /tmp/web.jar \
        -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" \
        -X "$1" ${3:+-d "$3"} "$WEB$2"
}
web_field() { python3 -c "import json; print(json.load(open('/tmp/web.out'))$1)"; }
web_up() { curl -s -o /dev/null "$WEB/"; }

useradd -m -u 1001 kid
echo kid:password | chpasswd

echo "== Install"
if [ -n "${FROM_REV:-}" ]; then
    (cd /root/old && bash install.sh >/tmp/install-old.log 2>&1)
    check "install $FROM_REV" test $? -eq 0
    cmctl add-user kid >/dev/null 2>&1
    cmctl set-weekday-hours kid 0 23 >/dev/null 2>&1
    systemctl start child-minder
fi

(cd /root/new && bash install.sh >/tmp/install.log 2>&1)
check "install.sh succeeds" test $? -eq 0
check "service is running after install" wait_for 10 service_active
check "package installed under /usr/lib/child-minder" \
    test -f /usr/lib/child-minder/childminder/daemon.py
check "cmctl is a symlink into /usr/lib" \
    test "$(readlink /usr/bin/cmctl)" = /usr/lib/child-minder/cmctl.py
check "no daemon left in /usr/bin" test ! -e /usr/bin/child-minder.py
if [ -n "${FROM_REV:-}" ]; then
    check "update path taken" grep -q "performing update" /tmp/install.log
    # Some releases also copy the window to an empty weekend, which shows it
    # under "(all days)" rather than "Weekday (Mon-Fri)"
    check "schedule written by $FROM_REV is readable" \
        bash -c 'cmctl user-status kid | grep -q "00:00 - 23:00"'
    cmctl clear-user-hours kid >/dev/null
fi

# Short intervals keep the run to a few minutes
python3 - "$CONFIG" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    config = json.load(f)
config["monitored_users"] = ["kid"]
config["check_interval"] = 2
config["user_control"]["check_interval"] = 5
config["warning_intervals"] = [30]
config["limited_processes"] = {"testapp": 1}
config["blocked_processes"] = ["blockedapp"]
config["monitored_processes"] = ["trackedapp"]
config["user_daily_limits"] = {}
with open(sys.argv[1], "w") as f:
    json.dump(config, f, indent=2)
EOF
check "config reload" reload_and_wait
check "cmctl config" cmctl config

echo "== Signals right after start"
systemctl restart child-minder
systemctl reload child-minder
PID=$(systemctl show -p MainPID --value child-minder)
cmctl reset >/dev/null
sleep 2
check "reload and reset straight after start leave the daemon running" \
    test "$(systemctl show -p MainPID --value child-minder)" = "$PID" -a "$PID" != 0

echo "== Sandbox"
PID=$(systemctl show -p MainPID --value child-minder)
check "/etc is read-only inside the service" \
    bash -c "! nsenter -t $PID -m touch /etc/child-minder-probe"
check "state directory is writable inside the service" \
    nsenter -t "$PID" -m bash -c 'touch /var/lib/child-minder/probe && rm /var/lib/child-minder/probe'

echo "== Blocked application"
start_as_kid blockedapp
check "blocked process is terminated" wait_for 10 kid_stopped blockedapp
end_kid_processes

echo "== Application limit"
reset_usage
mark
start_as_kid testapp
check "warning under a minute says less than 1 minute" \
    wait_for 60 logged "testapp time limit (less than 1 minute remaining)"
check "process is terminated at its limit" wait_for 60 kid_stopped testapp
end_kid_processes

echo "== Daily screen time limit"
reset_usage
cmctl user-limit kid 1 >/dev/null
start_as_kid trackedapp
start_as_kid othersleep
check "session ends at the daily limit" wait_for 90 kid_has_no_processes
cmctl user-unlimit kid >/dev/null
end_kid_processes

echo "== Access hours"
reset_usage
start_as_kid othersleep
cmctl "deny-$(today_type)-access" kid >/dev/null
check "session ends outside access hours" wait_for 30 kid_has_no_processes
cmctl clear-user-hours kid >/dev/null
check "cleared hours show unrestricted" \
    bash -c 'cmctl user-status kid | grep -q "Access Hours: Unrestricted"'

echo "== Timed disable"
reset_usage
cmctl disable-user kid -t 0.005 -r functional >/dev/null
check "account is expired while disabled" account_expired
mark
check "daemon re-enables the account when the time is up" wait_for 60 account_open
# The expiry is cleared before passwd -u runs, so the account can show open
# a moment before the password lock is lifted
check "password lock is lifted" wait_for 10 password_unlocked
check "re-enable ran through systemd-run" \
    bash -c 'journalctl --no-pager -o cat | grep -q "child-minder account change"'

echo "== Timed disable while monitoring is off"
cmctl disable >/dev/null
cmctl disable-user kid -t 0.005 -r functional >/dev/null
check "account is expired while disabled" account_expired
check "account is re-enabled with monitoring off" wait_for 60 account_open
cmctl enable >/dev/null

echo "== Permanent disable"
reset_usage
start_as_kid othersleep
cmctl disable-user kid -r functional >/dev/null
check "disable ends the session" wait_for 10 kid_has_no_processes
check "account is expired" account_expired
cmctl enable-user kid >/dev/null
check "enable-user reopens the account" account_open

echo "== State across restart"
reset_usage
start_as_kid trackedapp
sleep 6
end_kid_processes
# Usage reaches state.json when the daemon stops, so each reading follows a
# stop; the second stop saves whatever the restarted daemon loaded.
usage_seconds() {
    python3 -c 'import json; print(json.load(open("/var/lib/child-minder/state.json"))["daily_usage"]["kid"]["trackedapp"])'
}
systemctl stop child-minder
BEFORE=$(usage_seconds)
systemctl start child-minder
wait_for 10 service_active
sleep 3
systemctl stop child-minder
check "usage survives a restart" test -n "$BEFORE" -a "$BEFORE" = "$(usage_seconds)"
systemctl start child-minder
wait_for 10 service_active

echo "== Broken config"
cp "$CONFIG" /root/config.good
echo "{broken" > "$CONFIG"
mark
systemctl reload child-minder
check "reload keeps the running config" wait_for 15 logged "keeping the configuration already in use"
check "service still running" service_active
check "cmctl reports the error" bash -c "cmctl config 2>&1 | grep -q 'Invalid JSON'"
mark
systemctl restart child-minder
check "restart falls back to last good config" \
    wait_for 15 logged "Falling back to last known good configuration"
check "service running on fallback config" service_active
cp /root/config.good "$CONFIG"
check "config reload" reload_and_wait

echo "== Web UI"
check "web UI is running" wait_for 15 web_up
check "broker socket is listening" systemctl is-active --quiet child-minder-broker.socket
WEB_PID=$(systemctl show -p MainPID --value child-minder-web)
check "web UI runs as child-minder-web" \
    test "$(ps -o uid= -p "$WEB_PID" | tr -d ' ')" = "$(id -u child-minder-web)"
check "web UI has no capabilities" \
    bash -c "grep -q '^CapEff:[[:space:]]*0*$' /proc/$WEB_PID/status"
check "login is refused before a password is set" \
    test "$(web POST /api/login '{"password":"x"}')" = 503
echo secret | cmctl web-password >/dev/null
check "web.json is readable by the web UI group only" \
    test "$(stat -c '%a %G' /etc/child-minder/web.json)" = "640 child-minder-web"
check "wrong password is refused" test "$(web POST /api/login '{"password":"x"}')" = 401
check "login succeeds" test "$(web POST /api/login '{"password":"secret"}')" = 200
CSRF=$(web_field '["csrf"]')
check "overview lists the monitored user" \
    bash -c "[ \"\$(curl -s -b /tmp/web.jar $WEB/api/overview | python3 -c 'import json,sys; print([u[\"name\"] for u in json.load(sys.stdin)[\"users\"]])')\" = \"['kid']\" ]"
check "change without the CSRF token is refused" \
    test "$(CSRF=wrong web POST /api/commands '{"commands":[{"command":"block","args":["webapp"]}]}')" = 403
check "block through the web UI" \
    test "$(web POST /api/commands '{"commands":[{"command":"block","args":["webapp"]}]}')" = 200
check "block reached config.json" grep -q '"webapp"' "$CONFIG"
check "failing command is reported" \
    test "$(web POST /api/commands '{"commands":[{"command":"unblock","args":["nothing"]}]}')" = 422
check "lock of an unmonitored user is refused" \
    test "$(web POST /api/commands '{"commands":[{"command":"disable-user","args":["root"]}]}')" = 403
check "lock through the web UI" \
    test "$(web POST /api/commands '{"commands":[{"command":"disable-user","args":["kid"],"options":{"hours":1,"reason":"web"}}]}')" = 200
check "web UI lock expires the account" account_expired
check "unlock through the web UI" \
    test "$(web POST /api/commands '{"commands":[{"command":"enable-user","args":["kid"]}]}')" = 200
check "web UI unlock reopens the account" account_open
check "change is logged with the client address" \
    bash -c "journalctl --no-pager -o cat | grep -q 'client 127.0.0.1 ran: cmctl block -- webapp'"
check "other users cannot reach the broker" \
    bash -c "! su -s /bin/bash nobody -c \"python3 -c 'import socket; s = socket.socket(socket.AF_UNIX); s.connect(\\\"/run/child-minder-broker.sock\\\")'\" 2>/dev/null"

su kid -s /bin/bash -c "setsid xvfb-run -n 99 -f /home/kid/.Xauthority sleep 1000 >/dev/null 2>&1 &"
wait_for 10 bash -c "pgrep -u kid -x Xvfb"
sleep 1
check "screenshot of the user's display" \
    test "$(web POST /api/screenshot '{"user":"kid"}')" = 200
check "screenshot is a PNG of display :99" \
    bash -c "python3 -c \"import base64, json; r = json.load(open('/tmp/web.out')); assert r['display'] == ':99'; assert base64.b64decode(r['png']).startswith(b'\\\\x89PNG')\""
check "screenshot of an unmonitored user is refused" \
    test "$(web POST /api/screenshot '{"user":"root"}')" = 403
end_kid_processes
check "logout ends the session" test "$(web POST /api/logout)" = 200
check "session is gone after logout" test "$(web GET /api/overview)" = 401

echo "== Uninstall"
cmctl disable-user kid -r functional >/dev/null
(cd /root/new && printf 'y\nn\nn\n' | bash uninstall.sh >/tmp/uninstall.log 2>&1)
check "uninstall.sh succeeds" test $? -eq 0
check "uninstall re-enables disabled accounts" account_open
check "service is gone" bash -c '! systemctl cat child-minder'
check "files are removed" bash -c 'test ! -e /usr/lib/child-minder && test ! -e /usr/bin/cmctl && test ! -e /usr/bin/cmctl.py'
check "web UI units are gone" bash -c '! systemctl cat child-minder-web && ! systemctl cat child-minder-broker.socket'
check "web UI user is removed" bash -c '! getent passwd child-minder-web'

echo ""
echo "${PASSED} passed, ${#FAILURES[@]} failed"
if [ ${#FAILURES[@]} -gt 0 ]; then
    printf '  failed: %s\n' "${FAILURES[@]}"
    echo ""
    echo "Last daemon log lines:"
    tail -n 30 "$LOG" 2>/dev/null
    echo ""
    echo "Last service journal lines:"
    journalctl -u child-minder --no-pager -n 40 -o short-precise
    exit 1
fi
