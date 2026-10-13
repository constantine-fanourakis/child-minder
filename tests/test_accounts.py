"""Tests for account locking and unlocking"""

import unittest
from unittest import mock

from childminder import accounts


class Runner:
    def __init__(self, failing=()):
        self.calls = []
        self.failing = failing

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] in self.failing:
            return mock.Mock(returncode=1, stderr=f"{argv[0]}: refused\n")
        return mock.Mock(returncode=0, stderr="")


class AccountsTest(unittest.TestCase):
    def test_lock_expires_then_locks_the_password(self):
        run = Runner()
        self.assertIsNone(accounts.lock("kid", run))
        self.assertEqual(run.calls, [['usermod', '--expiredate', '1', 'kid'],
                                     ['passwd', '-l', 'kid']])

    def test_failed_expiration_is_an_error_and_skips_the_password(self):
        run = Runner(failing=('usermod',))
        self.assertEqual(accounts.lock("kid", run), "usermod: refused")
        self.assertEqual(len(run.calls), 1)

    def test_unlock_reports_a_password_that_stayed_locked(self):
        self.assertEqual(accounts.unlock("kid", Runner()), (None, None))
        self.assertEqual(accounts.unlock("kid", Runner(failing=('passwd',))),
                         (None, "passwd: refused"))
        self.assertEqual(accounts.unlock("kid", Runner(failing=('usermod',))),
                         ("usermod: refused", None))

    def test_a_command_that_cannot_run_is_an_error(self):
        self.assertEqual(accounts.lock("kid", lambda argv: None), "command could not be run")
