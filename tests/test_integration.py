"""Integration tests that run a monitoring pass against real processes.

Each test starts a copy of sleep under a name nothing else uses and points
the daemon at the current user, so a pass can only ever act on that copy.
They run unprivileged, and skip when the current user is not one the daemon
would monitor.
"""

import os
import shutil
import subprocess
import time
import unittest

import psutil

from tests.support import MinderTestCase, current_username

BLOCKED_NAME = "cmtest-blocked"


@unittest.skipIf(os.getuid() < 1000, "needs to run as a regular (UID >= 1000) user")
class RealProcessTest(MinderTestCase):
    def setUp(self):
        super().setUp()
        self.exe = self.tmp / BLOCKED_NAME
        shutil.copy(shutil.which("sleep"), self.exe)

    def track(self, pid: int) -> psutil.Process:
        proc = psutil.Process(pid)
        self.addCleanup(self._kill, proc)
        deadline = time.monotonic() + 5
        while proc.name() != BLOCKED_NAME:
            if time.monotonic() > deadline:
                self.fail(f"process {pid} never exec'd {BLOCKED_NAME}")
            time.sleep(0.01)
        return proc

    @staticmethod
    def _kill(proc: psutil.Process):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass

    def spawn_detached(self) -> psutil.Process:
        """Start the copy as a grandchild, so it is not one of the test process's children"""
        out = subprocess.run(['sh', '-c', '"$0" 60 >/dev/null 2>&1 & echo $!', str(self.exe)],
                             capture_output=True, text=True, check=True).stdout
        return self.track(int(out))

    def spawn_child(self) -> psutil.Process:
        popen = subprocess.Popen([str(self.exe), "60"])
        self.addCleanup(popen.wait)
        return self.track(popen.pid)

    def blocking_minder(self):
        return self.make_minder({"monitored_users": [current_username()],
                                 "blocked_processes": [BLOCKED_NAME]})

    def test_blocked_process_is_terminated(self):
        proc = self.spawn_detached()
        with self.assertLogs("childminder", "WARNING") as logs:
            self.blocking_minder().monitor_processes(5)
        self.assertFalse(proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE)
        self.assertTrue(any("Blocked application" in line for line in logs.output))

    def test_daemon_children_are_spared(self):
        proc = self.spawn_child()
        self.blocking_minder().monitor_processes(5)
        self.assertTrue(proc.is_running())
        self.assertNotEqual(proc.status(), psutil.STATUS_ZOMBIE)

    def test_limit_charges_and_terminates(self):
        proc = self.spawn_detached()
        minder = self.make_minder({"monitored_users": [current_username()],
                                   "limited_processes": {"cmtest-*": 1}})
        minder.emit_warning = lambda *args: None

        minder.monitor_processes(30)
        self.assertEqual(minder.get_usage(current_username(), "cmtest-*"), 30)
        self.assertTrue(proc.is_running())

        minder.monitor_processes(30)
        proc.wait(timeout=10)
