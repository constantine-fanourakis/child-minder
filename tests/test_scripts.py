"""Test uninstall shutdown ordering with a fake systemctl, never live services."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class UninstallShutdownTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls_path = Path(tmp.name) / "calls"
        source = (ROOT / "uninstall.sh").read_text()
        # Execute only the service-shutdown section. Account changes, file
        # removal and the rest of uninstall.sh must never run on the host.
        self.shutdown = source.split("# Stop and disable services\n", 1)[1].split(
            "# Re-enable any disabled users\n", 1)[0]

    def run_shutdown(self, missing="", failed_stop="", failed_show=""):
        self.calls_path.write_text("")
        env = dict(os.environ, CALL_LOG=str(self.calls_path), MISSING_UNIT=missing,
                   FAILED_STOP_UNIT=failed_stop, FAILED_SHOW_UNIT=failed_show)
        fake = r'''
systemctl() {
    printf '%s\n' "$*" >> "$CALL_LOG"
    if [ "$1" = show ]; then
        local unit=${!#}
        if [ "$unit" = "$MISSING_UNIT" ]; then
            printf 'not-found\n'
            return 1
        fi
        if [ "$unit" = "$FAILED_SHOW_UNIT" ]; then
            return 1
        fi
        printf 'loaded\n'
    elif [ "$1" = stop ] && [ "$2" = "$FAILED_STOP_UNIT" ]; then
        return 1
    fi
    return 0
}
'''
        result = subprocess.run(['bash'], input="set -e\n" + fake + self.shutdown
                                + "printf 'accounts-cleanup\\n'\n",
                                env=env, capture_output=True, text=True, timeout=5)
        return result, self.calls_path.read_text().splitlines()

    def test_listener_and_accepted_requests_stop_before_account_cleanup(self):
        result, calls = self.run_shutdown()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("accounts-cleanup", result.stdout)
        web = calls.index("stop child-minder-web.service")
        socket = calls.index("stop child-minder-broker.socket")
        requests = calls.index("stop child-minder-broker@*.service")
        daemon = calls.index("stop child-minder.service")
        self.assertLess(web, socket)
        self.assertLess(socket, requests)
        self.assertLess(requests, daemon)

    def test_missing_units_from_older_installs_are_harmless(self):
        for unit in ("child-minder-web.service", "child-minder-broker.socket"):
            with self.subTest(unit=unit):
                result, calls = self.run_shutdown(missing=unit)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("accounts-cleanup", result.stdout)
                self.assertNotIn("stop " + unit, calls)
                self.assertIn("stop child-minder-broker@*.service", calls)

    def test_stop_failures_abort_before_account_cleanup(self):
        for unit in ("child-minder-web.service", "child-minder-broker.socket",
                     "child-minder-broker@*.service", "child-minder.service"):
            with self.subTest(unit=unit):
                result, _ = self.run_shutdown(failed_stop=unit)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("accounts-cleanup", result.stdout)

    def test_manager_lookup_failure_is_not_mistaken_for_a_missing_unit(self):
        result, _ = self.run_shutdown(failed_show="child-minder-web.service")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("accounts-cleanup", result.stdout)
