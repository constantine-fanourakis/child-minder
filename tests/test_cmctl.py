"""Tests for the cmctl command-line interface, run through its main()"""

import fcntl
import io
import json
import os
import unittest
from unittest import mock

from childminder import webauth
from tests.support import CmctlTestCase, cmctl, current_username


class PrivilegeTest(CmctlTestCase):
    def test_state_changing_command_requires_root(self):
        self.euid = 1000
        out = self.run_cmctl("block", "steam")
        self.assertIn("must be run as root", out)
        self.assertEqual(self.read_json(self.config_path).get("blocked_processes"), None)

    def test_read_only_command_runs_without_root(self):
        self.euid = 1000
        out = self.run_cmctl("config")
        self.assertIn("Current Configuration", out)


class UnreadableDataTest(CmctlTestCase):
    """Data directories are root-only, so an unprivileged read must say so"""

    def fake_run_result(self, argv):
        if argv[0] == 'tail':
            return mock.Mock(returncode=1, stdout='',
                             stderr=f"tail: cannot open '{argv[-1]}' for reading: "
                                    f"Permission denied")
        return super().fake_run_result(argv)

    def test_logs_reports_unreadable_log(self):
        self.euid = 1000
        out = self.run_cmctl("logs")
        self.assertIn("Permission denied", out)
        self.assertIn("sudo cmctl", out)

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 000 directory")
    def test_user_status_reports_unreadable_state(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        self.user_control_path = locked / "user_control.json"
        self.user_control_path.write_text("{}")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.euid = 1000
        with mock.patch.object(cmctl, 'USER_CONTROL_PATH', str(self.user_control_path)):
            out = self.run_cmctl("user-status")
        self.assertIn("Permission denied", out)
        self.assertIn("sudo cmctl", out)
        self.assertNotIn("Errno", out)


    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 000 directory")
    def test_usage_reports_unreadable_state_even_if_exists_says_missing(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        state_path = locked / "state.json"
        state_path.write_text("{}")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.euid = 1000
        with mock.patch.object(cmctl, 'STATE_PATH', str(state_path)), \
                mock.patch.object(cmctl.Path, 'exists', return_value=False):
            out = self.run_cmctl("usage")
        self.assertIn("Permission denied", out)
        self.assertNotIn("No usage data", out)


class ExitStatusTest(CmctlTestCase):
    """cmctl exits 1 when a command changed nothing because it could not"""

    def assertExits(self, status, *argv):
        self.run_cmctl(*argv)
        self.assertEqual(self.exit_status, status, argv)

    def test_successful_commands_exit_zero(self):
        self.assertExits(0, "block", "steam")
        self.assertExits(0, "config")
        self.assertExits(0, "set-weekday-hours", "kid", "15", "20")

    def test_reaching_the_requested_state_already_exits_zero(self):
        self.run_cmctl("block", "steam")
        self.assertExits(0, "block", "steam")
        self.assertExits(0, "clear-weekend-windows", "kid")
        self.run_cmctl("deny-weekend-access", "kid")
        self.assertExits(0, "deny-weekend-access", "kid")

    def test_removing_what_is_not_there_exits_one(self):
        self.assertExits(1, "unblock", "steam")
        self.assertExits(1, "unlimit", "steam")
        self.assertExits(1, "remove-from-group", "games", "steam")
        self.assertExits(1, "group-unlimit", "games")
        self.assertExits(1, "user-unlimit", "kid")
        self.assertExits(1, "remove-user", "kid")
        self.assertExits(1, "remove-weekday-window", "kid", "15", "20")

    def test_invalid_input_exits_one(self):
        self.assertExits(1, "limit", "steam", "-5")
        self.assertExits(1, "set-weekday-hours", "kid", "15", "15")
        self.assertExits(1, "disable-user", current_username(), "-t", "0")
        self.assertExits(1, "enable-user", current_username())
        self.assertExits(1, "user-status", "no-such-user-cmtest")

    def test_failed_system_step_exits_one(self):
        self.fake_run_result = lambda argv: mock.Mock(
            returncode=1 if argv[0] == 'usermod' else 0, stdout='', stderr='denied')
        self.assertExits(1, "disable-user", current_username())

    def test_unreadable_file_exits_one(self):
        self.write_config({})
        self.config_path.write_text("{broken")
        self.assertExits(1, "block", "steam")


class ConfigCommandTest(CmctlTestCase):
    def test_block_and_unblock(self):
        self.run_cmctl("block", "steam*")
        self.assertEqual(self.read_json(self.config_path)["blocked_processes"], ["steam*"])
        self.assertIn(['systemctl', 'reload', 'child-minder'], self.run_calls)

        self.run_cmctl("unblock", "steam*")
        self.assertEqual(self.read_json(self.config_path)["blocked_processes"], [])

    def test_process_limit(self):
        self.run_cmctl("limit", "minecraft*", "60")
        self.assertEqual(self.read_json(self.config_path)["limited_processes"],
                         {"minecraft*": 60})
        self.run_cmctl("unlimit", "minecraft*")
        self.assertEqual(self.read_json(self.config_path)["limited_processes"], {})

    def test_saving_keeps_config_private(self):
        os.chmod(self.config_path, 0o600)
        old_umask = os.umask(0o022)
        try:
            self.run_cmctl("block", "steam*")
        finally:
            os.umask(old_umask)
        self.assertEqual(os.stat(self.config_path).st_mode & 0o777, 0o600)

    def test_negative_limit_is_rejected(self):
        for command in (("limit", "minecraft*"), ("group-limit", "games"),
                        ("user-limit", "kid")):
            out = self.run_cmctl(*command, "-5")
            self.assertIn("zero or more", out)
        config = self.read_json(self.config_path)
        for key in ("limited_processes", "group_limits", "user_daily_limits"):
            self.assertNotIn(key, config)

    def test_zero_limit_is_saved_and_shown(self):
        self.run_cmctl("add-to-group", "games", "steam*")
        self.run_cmctl("group-limit", "games", "0")
        self.run_cmctl("user-weekday-limit", "kid", "0")
        config = self.read_json(self.config_path)
        self.assertEqual(config["group_limits"], {"games": 0})
        self.assertEqual(config["user_daily_limits"], {"kid": {"weekday": 0}})
        out = self.run_cmctl("config")
        self.assertIn("games (0 min/day)", out)
        self.assertIn("kid: weekday: 0 min", out)

    def test_usage_shows_remaining_time_under_a_zero_limit(self):
        self.write_config({"group_limits": {"games": 0}})
        with open(self.state_path, 'w') as f:
            json.dump({"group_usage": {"kid": {"games": 60}}}, f)
        out = self.run_cmctl("usage")
        self.assertIn("(Limit: 0 min, Remaining: 0 min)", out)

    def test_config_reports_invalid_entries(self):
        self.write_config({"group_limits": {"games": "90"}})
        out = self.run_cmctl("config")
        self.assertIn("Warning: Ignoring invalid group_limits entry 'games'", out)

    def test_groups_and_group_limits(self):
        self.run_cmctl("add-to-group", "games", "steam*")
        self.run_cmctl("group-limit", "games", "90")
        config = self.read_json(self.config_path)
        self.assertEqual(config["process_groups"], {"games": ["steam*"]})
        self.assertEqual(config["group_limits"], {"games": 90})

        self.run_cmctl("remove-from-group", "games", "steam*")
        self.assertEqual(self.read_json(self.config_path)["process_groups"], {})

    def test_user_daily_limits(self):
        self.run_cmctl("user-limit", "kid", "120")
        self.run_cmctl("user-weekend-limit", "kid", "240")
        self.assertEqual(self.read_json(self.config_path)["user_daily_limits"],
                         {"kid": {"weekday": 120, "weekend": 240}})
        self.run_cmctl("user-unlimit", "kid")
        self.assertEqual(self.read_json(self.config_path)["user_daily_limits"], {})

    def test_monitored_users(self):
        self.run_cmctl("add-user", "kid")
        self.assertEqual(self.read_json(self.config_path)["monitored_users"], ["kid"])
        self.run_cmctl("remove-user", "kid")
        self.assertEqual(self.read_json(self.config_path)["monitored_users"], [])

    def test_mutators_survive_wrong_typed_entries(self):
        self.write_config({"blocked_processes": "steam"})
        out = self.run_cmctl("block", "steam")
        self.assertIn("Added 'steam'", out)
        self.assertEqual(self.read_json(self.config_path)["blocked_processes"],
                         ["steam"])

        self.write_config({"blocked_processes": "steam"})
        self.run_cmctl("block", "minecraft")
        self.assertEqual(self.read_json(self.config_path)["blocked_processes"],
                         ["minecraft"])

    def test_user_limit_drops_only_the_invalid_day(self):
        self.write_config({"user_daily_limits": {"kid": {"weekday": "x"}}})
        out = self.run_cmctl("user-weekend-limit", "kid", "240")
        self.assertIn("Set weekend", out)
        self.assertEqual(self.read_json(self.config_path)["user_daily_limits"],
                         {"kid": {"weekend": 240}})
        self.assertNotIn("Ignoring invalid user_daily_limits",
                         self.run_cmctl("config"))

    def test_enable_and_disable_monitoring(self):
        self.run_cmctl("disable")
        self.assertFalse(self.read_json(self.config_path)["enabled"])
        self.run_cmctl("enable")
        self.assertTrue(self.read_json(self.config_path)["enabled"])


class ResetTest(CmctlTestCase):
    def setUp(self):
        super().setUp()
        self.service_active = True

    def fake_run_result(self, argv):
        if argv[:2] == ['systemctl', 'is-active']:
            return mock.Mock(returncode=0 if self.service_active else 3, stdout='', stderr='')
        return super().fake_run_result(argv)

    def test_running_service_is_signalled(self):
        self.run_cmctl("reset")
        kills = [c for c in self.run_calls if c[:2] == ['systemctl', 'kill']]
        self.assertEqual(len(kills), 1)
        self.assertIn('--signal=SIGUSR1', kills[0])
        self.assertFalse(self.state_path.exists())

    def test_signal_reaches_only_the_main_process(self):
        self.run_cmctl("reset")
        kills = [c for c in self.run_calls if c[:2] == ['systemctl', 'kill']]
        self.assertIn('--kill-who=main', kills[0])

    def test_stopped_service_has_state_file_cleared(self):
        self.service_active = False
        with open(self.state_path, 'w') as f:
            json.dump({"daily_usage": {"kid": {"steam*": 100}}}, f)
        self.run_cmctl("reset")
        state = self.read_json(self.state_path)
        self.assertEqual(state["daily_usage"], {})
        self.assertEqual(state["user_daily_usage"], {})


class AccountCommandTest(CmctlTestCase):
    def test_disable_user_expires_account_and_records_it(self):
        user = current_username()
        self.run_cmctl("disable-user", user, "-r", "grounded", "-t", "2")
        self.assertIn(['usermod', '--expiredate', '1', user], self.run_calls)
        info = self.read_json(self.user_control_path)["disabled_users"][user]
        self.assertEqual(info["reason"], "grounded")
        self.assertIn("re_enable_at", info)
        self.assertTrue(self.read_json(self.config_path)["user_control"]["enabled"])

    def test_enable_user_clears_record(self):
        user = current_username()
        self.run_cmctl("disable-user", user)
        self.run_cmctl("enable-user", user)
        self.assertIn(['usermod', '--expiredate', '', user], self.run_calls)
        self.assertEqual(self.read_json(self.user_control_path)["disabled_users"], {})

    def test_disable_system_account_is_refused(self):
        out = self.run_cmctl("disable-user", "root")
        self.assertIn("system account", out)
        self.assertEqual(self.run_calls, [])
        self.assertFalse(self.user_control_path.exists())

    def test_enable_system_account_is_refused(self):
        out = self.run_cmctl("enable-user", "root")
        self.assertIn("system account", out)
        self.assertEqual(self.run_calls, [])
        self.assertFalse(self.user_control_path.exists())

    def test_enable_user_who_is_not_disabled_is_refused(self):
        out = self.run_cmctl("enable-user", current_username())
        self.assertIn("not disabled", out)
        self.assertEqual(self.run_calls, [])

    def test_enable_unknown_user_is_refused(self):
        out = self.run_cmctl("enable-user", "no-such-user-cmtest")
        self.assertIn("does not exist", out)
        self.assertEqual(self.run_calls, [])

    def test_unrunnable_lock_command_leaves_no_record(self):
        def fake_run_result(argv):
            if argv[0] == 'usermod':
                raise FileNotFoundError("usermod missing")
            return mock.Mock(returncode=0, stdout='', stderr='')
        self.fake_run_result = fake_run_result
        out = self.run_cmctl("disable-user", current_username())
        self.assertIn("Failed to disable account", out)
        self.assertEqual(self.read_json(self.user_control_path)["disabled_users"], {})

    def test_non_positive_duration_is_refused(self):
        for hours in ("0", "0.0", "-1", "nan", "inf"):
            out = self.run_cmctl("disable-user", current_username(), "-t", hours)
            self.assertIn("must be a positive number", out)
        self.assertEqual(self.run_calls, [])
        self.assertFalse(self.user_control_path.exists())

    def test_unreadable_record_leaves_account_unlocked(self):
        self.user_control_path.write_text("{not json")
        out = self.run_cmctl("disable-user", current_username(), "-t", "2")
        self.assertIn("Invalid JSON", out)
        self.assertEqual(self.run_calls, [])

    def test_failed_lock_leaves_no_record(self):
        self.fake_run_result = lambda argv: mock.Mock(
            returncode=1 if argv[0] == 'usermod' else 0, stdout='', stderr='denied')
        out = self.run_cmctl("disable-user", current_username())
        self.assertIn("Failed to disable account", out)
        self.assertEqual(self.read_json(self.user_control_path)["disabled_users"], {})

    def test_min_uid_below_uid_min_does_not_expose_root(self):
        self.write_config({"min_uid": 0})
        out = self.run_cmctl("disable-user", "root")
        self.assertIn("system account", out)
        self.assertEqual(self.run_calls, [])

    def test_account_is_expired_while_holding_the_user_control_lock(self):
        lock_path = self.user_control_path.with_name("user_control.json.lock")
        held = []

        def fake_run_result(argv):
            if argv[0] == 'usermod':
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
                finally:
                    os.close(fd)
            return mock.Mock(returncode=0, stdout='', stderr='')
        self.fake_run_result = fake_run_result
        self.run_cmctl("disable-user", current_username(), "-t", "1")
        self.run_cmctl("enable-user", current_username())
        self.assertEqual(held, [True, True])

    def test_disable_unknown_user_is_refused(self):
        out = self.run_cmctl("disable-user", "no-such-user-cmtest")
        self.assertIn("does not exist", out)
        self.assertFalse(self.user_control_path.exists())


class WebPasswordTest(CmctlTestCase):
    def set_password(self, line: str) -> str:
        with mock.patch('sys.stdin', io.StringIO(line)):
            return self.run_cmctl("web-password")

    def test_password_from_stdin_is_stored_hashed(self):
        out = self.set_password("secret\n")
        self.assertIn("password set", out)
        self.assertEqual(self.exit_status, 0)
        stored = self.read_json(self.web_config_path)["password"]
        self.assertNotIn("secret", json.dumps(stored))
        self.assertTrue(webauth.verify_password("secret", stored))
        self.assertEqual(os.stat(self.web_config_path).st_mode & 0o777, 0o600)

    def test_web_ui_group_may_read_the_file(self):
        group = mock.Mock(gr_gid=os.getgid())
        with mock.patch.object(cmctl.grp, 'getgrnam', return_value=group) as getgrnam:
            self.set_password("secret\n")
        getgrnam.assert_called_with("child-minder-web")
        info = os.stat(self.web_config_path)
        self.assertEqual(info.st_mode & 0o777, 0o640)
        self.assertEqual(info.st_gid, os.getgid())

    def test_without_the_web_ui_group_the_file_stays_private(self):
        with mock.patch.object(cmctl.grp, 'getgrnam', side_effect=KeyError):
            self.set_password("secret\n")
        self.assertEqual(os.stat(self.web_config_path).st_mode & 0o777, 0o600)

    def test_other_settings_are_kept(self):
        self.web_config_path.write_text('{"port": 9000}')
        self.set_password("secret\n")
        self.assertEqual(self.read_json(self.web_config_path)["port"], 9000)

    def test_empty_password_is_refused(self):
        out = self.set_password("\n")
        self.assertIn("must not be empty", out)
        self.assertEqual(self.exit_status, 1)
        self.assertFalse(self.web_config_path.exists())

    def test_terminal_entry_must_match(self):
        with mock.patch('sys.stdin', mock.Mock(isatty=lambda: True)), \
                mock.patch.object(cmctl.getpass, 'getpass', side_effect=["one", "two"]):
            out = self.run_cmctl("web-password")
        self.assertIn("do not match", out)
        self.assertFalse(self.web_config_path.exists())

    def test_requires_root(self):
        self.euid = 1000
        out = self.set_password("secret\n")
        self.assertIn("must be run as root", out)
        self.assertFalse(self.web_config_path.exists())


class ScheduleCommandTest(CmctlTestCase):
    def schedule(self, user="kid"):
        return self.read_json(self.user_control_path)["daily_schedules"].get(user)

    def test_set_weekday_hours_leaves_weekend_open(self):
        out = self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.assertEqual(self.schedule(), {
            "weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []})
        self.assertIn("Weekend access is unrestricted", out)
        self.assertTrue(self.read_json(self.config_path)["user_control"]["enabled"])

    def test_set_user_hours_also_fills_an_unset_weekend(self):
        self.run_cmctl("set-user-hours", "kid", "8", "21")
        window = [{"start_hour": 8, "end_hour": 21}]
        self.assertEqual(self.schedule(), {"weekday": window, "weekend": window})

    def test_set_user_hours_keeps_a_set_or_denied_weekend(self):
        self.run_cmctl("set-weekend-hours", "kid", "10", "22")
        self.run_cmctl("set-user-hours", "kid", "8", "21")
        self.assertEqual(self.schedule()["weekend"], [{"start_hour": 10, "end_hour": 22}])

        self.run_cmctl("deny-weekend-access", "kid2")
        self.run_cmctl("set-user-hours", "kid2", "8", "21")
        self.assertEqual(self.schedule("kid2")["weekend"], [])
        self.assertEqual(self.schedule("kid2")["no_access"], ["weekend"])

    def test_set_hours_replaces_existing_windows(self):
        self.run_cmctl("add-weekend-window", "kid", "9", "12")
        self.run_cmctl("add-weekend-window", "kid", "14", "18")
        self.run_cmctl("set-weekend-hours", "kid", "10", "22")
        self.assertEqual(self.schedule()["weekend"], [{"start_hour": 10, "end_hour": 22}])

    def test_invalid_hours_are_rejected(self):
        out = self.run_cmctl("set-weekday-hours", "kid", "15", "15")
        self.assertIn("cannot be the same", out)
        out = self.run_cmctl("set-weekday-hours", "kid", "24", "6")
        self.assertIn("between 0 and 23", out)
        self.assertFalse(self.user_control_path.exists())

    def test_windows_are_kept_sorted_and_unique(self):
        self.run_cmctl("add-weekday-window", "kid", "15", "20")
        self.run_cmctl("add-weekday-window", "kid", "7", "8")
        out = self.run_cmctl("add-weekday-window", "kid", "7", "8")
        self.assertIn("already exists", out)
        self.assertEqual(self.schedule()["weekday"], [
            {"start_hour": 7, "end_hour": 8}, {"start_hour": 15, "end_hour": 20}])

    def test_remove_window(self):
        self.run_cmctl("add-weekday-window", "kid", "7", "8")
        self.run_cmctl("add-weekday-window", "kid", "15", "20")
        self.run_cmctl("remove-weekday-window", "kid", "7", "8")
        self.assertEqual(self.schedule()["weekday"], [{"start_hour": 15, "end_hour": 20}])

    def test_removing_last_window_denies_the_day_type(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "17")
        out = self.run_cmctl("remove-weekday-window", "kid", "15", "17")
        self.assertIn("no weekday access", out)
        sched = self.schedule()
        self.assertEqual(sched["weekday"], [])
        self.assertEqual(sched["no_access"], ["weekday"])

    def test_clearing_one_day_type_keeps_the_other(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("set-weekend-hours", "kid", "9", "22")
        self.run_cmctl("clear-weekday-windows", "kid")
        self.assertEqual(self.schedule(), {
            "weekday": [], "weekend": [{"start_hour": 9, "end_hour": 22}]})

    def test_clearing_last_day_type_drops_entry(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("clear-weekday-windows", "kid")
        self.assertIsNone(self.schedule())

    def test_clear_user_hours(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("set-weekend-hours", "kid", "9", "22")
        self.run_cmctl("clear-user-hours", "kid")
        self.assertIsNone(self.schedule())

    def test_user_status_shows_schedule(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        out = self.run_cmctl("user-status")
        self.assertIn("Weekday (Mon-Fri): 15:00 - 20:00", out)
        self.assertIn("Weekend (Sat-Sun): Unrestricted", out)

    def test_deny_day_type(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("add-weekend-window", "kid", "9", "12")
        out = self.run_cmctl("deny-weekend-access", "kid")
        self.assertIn("no weekend access", out)
        self.assertEqual(self.schedule(), {
            "weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": [],
            "no_access": ["weekend"]})
        self.assertTrue(self.read_json(self.config_path)["user_control"]["enabled"])

    def test_deny_is_idempotent(self):
        self.run_cmctl("deny-weekday-access", "kid")
        out = self.run_cmctl("deny-weekday-access", "kid")
        self.assertIn("already has no weekday access", out)
        self.assertEqual(self.schedule()["no_access"], ["weekday"])

    def test_setting_hours_lifts_deny(self):
        self.run_cmctl("deny-weekend-access", "kid")
        self.run_cmctl("set-weekend-hours", "kid", "10", "22")
        self.assertEqual(self.schedule(), {
            "weekday": [], "weekend": [{"start_hour": 10, "end_hour": 22}]})

    def test_adding_window_lifts_deny(self):
        self.run_cmctl("deny-weekday-access", "kid")
        self.run_cmctl("add-weekday-window", "kid", "7", "8")
        self.assertNotIn("no_access", self.schedule())

    def test_clearing_denied_day_type_makes_it_unrestricted(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("deny-weekend-access", "kid")
        self.run_cmctl("clear-weekend-windows", "kid")
        self.assertEqual(self.schedule(), {
            "weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []})

    def test_clearing_keeps_entry_while_other_day_is_denied(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("deny-weekend-access", "kid")
        self.run_cmctl("clear-weekday-windows", "kid")
        self.assertEqual(self.schedule(), {
            "weekday": [], "weekend": [], "no_access": ["weekend"]})

    def test_user_status_shows_no_access(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.run_cmctl("deny-weekend-access", "kid")
        out = self.run_cmctl("user-status")
        self.assertIn("Weekend (Sat-Sun): No access", out)

    def test_user_status_shows_no_access_on_all_days(self):
        self.run_cmctl("deny-weekday-access", "kid")
        self.run_cmctl("deny-weekend-access", "kid")
        out = self.run_cmctl("user-status")
        self.assertIn("Access Hours: No access (all days)", out)
