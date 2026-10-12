"""Tests for the cmctl command-line interface, run through its main()"""

import json
import os
import unittest
from unittest import mock

from tests.support import CmctlTestCase, MinderTestCase, cmctl, current_username


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

    def test_non_positive_limit_is_rejected(self):
        out = self.run_cmctl("limit", "minecraft*", "0")
        self.assertIn("must be a positive number", out)
        self.assertNotIn("limited_processes", self.read_json(self.config_path))

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

    def test_disable_unknown_user_is_refused(self):
        out = self.run_cmctl("disable-user", "no-such-user-cmtest")
        self.assertIn("does not exist", out)
        self.assertFalse(self.user_control_path.exists())


class ScheduleCommandTest(CmctlTestCase):
    def schedule(self, user="kid"):
        return self.read_json(self.user_control_path)["daily_schedules"].get(user)

    def test_set_weekday_hours_leaves_weekend_open(self):
        out = self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.assertEqual(self.schedule(), {
            "weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []})
        self.assertIn("Weekend access is unrestricted", out)
        self.assertTrue(self.read_json(self.config_path)["user_control"]["enabled"])

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


class ScheduleMigrationTest(CmctlTestCase):
    WINDOW = {"start_hour": 15, "end_hour": 20}

    def write_user_control(self, data: dict):
        with open(self.user_control_path, 'w') as f:
            json.dump(data, f)

    def test_old_file_shows_empty_day_type_as_denied(self):
        self.write_user_control({"daily_schedules": {
            "kid": {"weekday": [self.WINDOW], "weekend": []}}})
        out = self.run_cmctl("user-status")
        self.assertIn("Weekend (Sat-Sun): No access", out)
        self.assertIn("clear-weekend-windows kid", out)

    def test_saving_stamps_current_format(self):
        self.run_cmctl("set-weekday-hours", "kid", "15", "20")
        self.assertEqual(self.read_json(self.user_control_path)["schedule_format"],
                         cmctl.SCHEDULE_FORMAT)

    def test_old_file_is_migrated_before_changes(self):
        self.write_user_control({"daily_schedules": {
            "kid": {"weekday": [self.WINDOW], "weekend": []}}})
        self.run_cmctl("add-weekday-window", "kid", "7", "8")
        schedule = self.read_json(self.user_control_path)["daily_schedules"]["kid"]
        self.assertEqual(schedule["no_access"], ["weekend"])


class MigrationParityTest(MinderTestCase):
    """The daemon and cmctl each migrate old files and must agree on the result"""

    def test_daemon_and_cmctl_migrate_identically(self):
        schedules = {
            "a": {"weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []},
            "b": {"weekday": [], "weekend": []},
            "c": {"start_hour": 8, "end_hour": 20},
            "d": {"weekday": {"start_hour": 8, "end_hour": 20},
                  "weekend": {"start_hour": 9, "end_hour": 22}},
            "e": {},
        }
        self.write_user_control({"daily_schedules": json.loads(json.dumps(schedules))})
        daemon_result = self.make_minder().user_control_state["daily_schedules"]

        manager = cmctl.ChildMinderManager()
        cmctl_state = {"daily_schedules": json.loads(json.dumps(schedules))}
        with mock.patch('builtins.print'):
            manager._migrate_schedules(cmctl_state)
        self.assertEqual(cmctl_state["daily_schedules"], daemon_result)
