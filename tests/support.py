"""Shared helpers for the child-minder test suite"""

import json
import logging
import os
import pwd
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from childminder import cli as cmctl
from childminder import config as cfg
from childminder import daemon as child_minder

# The host's login.defs is not consulted, so results do not depend on it
UID_RANGE = (1000, 60000)

# Keeps daemon records off stderr; assertLogs still captures them
logging.getLogger("childminder").addHandler(logging.NullHandler())


class FakeUids:
    def __init__(self, real: int):
        self.real = real


class FakeProc:
    """Stand-in for psutil.Process exposing what the daemon reads"""

    def __init__(self, pid: int, name: str, uid: int, ppid: int = 1,
                 status: str = "running"):
        self.pid = pid
        self._name = name
        self.info = {'pid': pid, 'name': name, 'uids': FakeUids(uid),
                     'ppid': ppid, 'status': status}

    def name(self):
        return self._name

    def username(self):
        return 'fake'


class MinderTestCase(unittest.TestCase):
    """Builds a ChildMinder whose files all live in a temporary directory"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_path = self.tmp / "config.json"
        self.state_path = self.tmp / "state.json"
        self.log_path = self.tmp / "minder.log"
        self.user_control_path = self.tmp / "user_control.json"

    def write_config(self, config):
        with open(self.config_path, 'w') as f:
            json.dump(config, f)

    def write_user_control(self, data: dict):
        with open(self.user_control_path, 'w') as f:
            json.dump(data, f)

    def make_minder(self, config: dict = None):
        """Construct a ChildMinder, leaving the test process's signal handlers intact.

        File logging is replaced by the module logger alone, so assertLogs sees
        every record and no handler is left open between tests.
        """
        # Every regular account is monitored unless a test names the users
        if config is not None:
            self.write_config({"monitored_users": [cfg.ALL_USERS], **config})
        elif not self.config_path.exists():
            self.write_config({"monitored_users": [cfg.ALL_USERS]})

        handlers = {s: signal.getsignal(s)
                    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in handlers.items()])

        def setup_logging(minder):
            minder.logger = logging.getLogger("childminder")

        with mock.patch.object(child_minder.ChildMinder, 'setup_logging', setup_logging), \
                mock.patch.object(cfg, 'read_uid_range', return_value=UID_RANGE):
            return child_minder.ChildMinder(
                str(self.config_path), str(self.state_path), str(self.log_path))


class CmctlTestCase(unittest.TestCase):
    """Points cmctl at a temporary directory and records the commands it runs"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_path = self.tmp / "config.json"
        self.state_path = self.tmp / "state.json"
        self.log_path = self.tmp / "minder.log"
        self.user_control_path = self.tmp / "user_control.json"
        self.web_config_path = self.tmp / "web.json"
        with open(self.config_path, 'w') as f:
            json.dump({"enabled": True}, f)

        for name, path in (('CONFIG_PATH', self.config_path),
                           ('STATE_PATH', self.state_path),
                           ('LOG_PATH', self.log_path),
                           ('USER_CONTROL_PATH', self.user_control_path),
                           ('WEB_CONFIG_PATH', self.web_config_path)):
            patcher = mock.patch.object(cmctl, name, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.run_calls = []
        patcher = mock.patch.object(cmctl.subprocess, 'run', side_effect=self._fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.euid = 0
        patcher = mock.patch.object(cmctl.os, 'geteuid', side_effect=lambda: self.euid)
        patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(cfg, 'read_uid_range', return_value=UID_RANGE)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The web UI's group is absent unless a test provides it
        patcher = mock.patch.object(cmctl.grp, 'getgrnam', side_effect=KeyError)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, config: dict):
        with open(self.config_path, 'w') as f:
            json.dump(config, f)

    def fake_run_result(self, argv):
        """Result returned for a command cmctl runs; override to simulate failures"""
        return mock.Mock(returncode=0, stdout='', stderr='')

    def _fake_run(self, argv, *args, **kwargs):
        self.run_calls.append(list(argv))
        return self.fake_run_result(argv)

    def run_cmctl(self, *argv) -> str:
        """Run cmctl's main() with argv, returning everything it printed.

        The exit status is left in self.exit_status.
        """
        out = []
        self.exit_status = 0
        with mock.patch('sys.argv', ['cmctl', *argv]), \
                mock.patch('builtins.print',
                           side_effect=lambda *a, **k: out.append(' '.join(map(str, a)))):
            try:
                cmctl.main()
            except SystemExit as e:
                self.exit_status = e.code
        return '\n'.join(out)

    def read_json(self, path: Path) -> dict:
        with open(path) as f:
            return json.load(f)


def current_username() -> str:
    return pwd.getpwuid(os.getuid()).pw_name
