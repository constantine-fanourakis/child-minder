"""Unit tests for the child-minder daemon's interfaces"""

import json
from datetime import datetime
from unittest import mock

from tests.support import FakeProc, MinderTestCase, child_minder

KID_UID = 1001


def frozen_datetime(moment: datetime):
    """A datetime class whose now() always returns moment"""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment
    return Frozen


# 2026-10-12 is a Monday, 2026-10-17 a Saturday
WEEKDAY = datetime(2026, 10, 12)
WEEKEND = datetime(2026, 10, 17)


class PatternMatchingTest(MinderTestCase):
    match = staticmethod(child_minder.ChildMinder.pattern_matches)

    def test_plain_pattern_matches_whole_name_only(self):
        self.assertTrue(self.match("steam", "steam"))
        self.assertFalse(self.match("steam", "steamwebhelper"))
        self.assertFalse(self.match("sh", "bash"))

    def test_matching_ignores_case(self):
        self.assertTrue(self.match("Steam", "STEAM"))
        self.assertTrue(self.match("minecraft*", "MinecraftLauncher"))

    def test_glob_characters_select_glob_matching(self):
        self.assertTrue(self.match("steam*", "steamwebhelper"))
        self.assertTrue(self.match("vl?", "vlc"))
        self.assertTrue(self.match("[mn]pv", "mpv"))
        self.assertFalse(self.match("steam*", "notsteam"))

    def test_directory_part_of_name_is_ignored(self):
        self.assertTrue(self.match("vlc", "/usr/bin/vlc"))

    def test_first_matching_group_wins(self):
        minder = self.make_minder({"process_groups": {
            "games": ["steam*"], "launchers": ["steam"]}})
        self.assertEqual(minder.get_process_group("steam"), "games")
        self.assertIsNone(minder.get_process_group("firefox"))

    def test_process_limit_returns_config_key_and_seconds(self):
        minder = self.make_minder({"limited_processes": {"minecraft*": 60}})
        self.assertEqual(minder.get_process_limit("minecraft-launcher"), ("minecraft*", 3600))
        self.assertEqual(minder.get_process_limit("firefox"), (None, None))


class ConfigTest(MinderTestCase):
    def test_missing_keys_are_filled_with_defaults(self):
        minder = self.make_minder({"blocked_processes": ["x"]})
        self.assertEqual(minder.config["blocked_processes"], ["x"])
        self.assertEqual(minder.config["check_interval"], 5)
        self.assertEqual(minder.config["user_control"],
                         {"enabled": False, "check_interval": 60})

    def test_partial_nested_dict_is_completed(self):
        minder = self.make_minder({"user_control": {"enabled": True}})
        self.assertEqual(minder.config["user_control"],
                         {"enabled": True, "check_interval": 60})

    def test_invalid_scalar_values_fall_back(self):
        minder = self.make_minder({"check_interval": -1, "min_uid": "1000"})
        self.assertEqual(minder.config["check_interval"], 5)
        self.assertEqual(minder.config["min_uid"], 1000)

    def test_wrong_container_type_falls_back(self):
        minder = self.make_minder({"blocked_processes": "steam", "group_limits": []})
        self.assertEqual(minder.config["blocked_processes"], [])
        self.assertEqual(minder.config["group_limits"], {})

    def test_non_dict_user_control_falls_back(self):
        minder = self.make_minder({"user_control": None})
        self.assertEqual(minder.config["user_control"], {})
        minder.enforce_user_control()

    def test_non_object_config_falls_back_to_last_good(self):
        self.make_minder({"blocked_processes": ["x"]})
        for content in ("[]", "null", "42"):
            self.config_path.write_text(content)
            minder = self.make_minder()
            self.assertEqual(minder.config["blocked_processes"], ["x"])

    def test_non_object_config_is_rejected_on_reload(self):
        minder = self.make_minder({"blocked_processes": ["x"]})
        self.config_path.write_text("[]")
        minder.reload_config()
        self.assertEqual(minder.config["blocked_processes"], ["x"])

    def test_successful_load_saves_last_good_copy(self):
        self.make_minder({"blocked_processes": ["x"]})
        with open(self.tmp / child_minder.LAST_GOOD_CONFIG_NAME) as f:
            self.assertEqual(json.load(f)["blocked_processes"], ["x"])

    def test_unparseable_config_falls_back_to_last_good(self):
        self.make_minder({"blocked_processes": ["x"]})
        self.config_path.write_text("{not json")
        minder = self.make_minder()
        self.assertEqual(minder.config["blocked_processes"], ["x"])

    def test_unparseable_config_without_fallback_exits(self):
        self.config_path.write_text("{not json")
        with self.assertRaises(SystemExit):
            self.make_minder()

    def test_reload_keeps_running_config_when_new_one_is_bad(self):
        minder = self.make_minder({"blocked_processes": ["x"]})
        self.config_path.write_text("{not json")
        minder.reload_config()
        self.assertEqual(minder.config["blocked_processes"], ["x"])

    def test_reload_applies_good_config(self):
        minder = self.make_minder({"blocked_processes": ["x"]})
        self.write_config({"blocked_processes": ["y"]})
        minder.reload_config()
        self.assertEqual(minder.config["blocked_processes"], ["y"])


class UserSelectionTest(MinderTestCase):
    def test_system_accounts_are_never_monitored(self):
        minder = self.make_minder({"monitored_users": []})
        self.assertFalse(minder.should_monitor_user("root", 0))
        self.assertFalse(minder.should_monitor_user("daemon", 999))

    def test_empty_list_monitors_every_human_user(self):
        minder = self.make_minder({"monitored_users": []})
        self.assertTrue(minder.should_monitor_user("kid", KID_UID))

    def test_listed_users_only(self):
        minder = self.make_minder({"monitored_users": ["kid"]})
        self.assertTrue(minder.should_monitor_user("kid", KID_UID))
        self.assertFalse(minder.should_monitor_user("parent", 1002))


class AccessHoursTest(MinderTestCase):
    def access_at(self, moment: datetime, schedule: dict) -> bool:
        minder = self.make_minder()
        minder.user_control_state = {"disabled_users": {},
                                     "daily_schedules": {"kid": schedule}}
        with mock.patch.object(child_minder, 'datetime', frozen_datetime(moment)):
            return minder.check_user_access_hours("kid")

    def test_user_without_schedule_is_unrestricted(self):
        minder = self.make_minder()
        self.assertTrue(minder.check_user_access_hours("kid"))

    def test_regular_window(self):
        schedule = {"weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []}
        self.assertTrue(self.access_at(WEEKDAY.replace(hour=15), schedule))
        self.assertTrue(self.access_at(WEEKDAY.replace(hour=19), schedule))
        self.assertFalse(self.access_at(WEEKDAY.replace(hour=20), schedule))
        self.assertFalse(self.access_at(WEEKDAY.replace(hour=9), schedule))

    def test_overnight_window(self):
        schedule = {"weekday": [], "weekend": [{"start_hour": 22, "end_hour": 6}]}
        self.assertTrue(self.access_at(WEEKEND.replace(hour=23), schedule))
        self.assertTrue(self.access_at(WEEKEND.replace(hour=5), schedule))
        self.assertFalse(self.access_at(WEEKEND.replace(hour=12), schedule))

    def test_any_of_several_windows_allows_access(self):
        schedule = {"weekday": [{"start_hour": 7, "end_hour": 8},
                                {"start_hour": 15, "end_hour": 20}], "weekend": []}
        self.assertTrue(self.access_at(WEEKDAY.replace(hour=7), schedule))
        self.assertFalse(self.access_at(WEEKDAY.replace(hour=12), schedule))

    def test_day_type_without_windows_is_unrestricted(self):
        schedule = {"weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []}
        self.assertTrue(self.access_at(WEEKEND.replace(hour=3), schedule))

    def test_legacy_flat_schedule_applies_to_all_days(self):
        schedule = {"start_hour": 8, "end_hour": 20}
        self.assertFalse(self.access_at(WEEKDAY.replace(hour=21), schedule))
        self.assertFalse(self.access_at(WEEKEND.replace(hour=21), schedule))
        self.assertTrue(self.access_at(WEEKEND.replace(hour=10), schedule))


class WarningTest(MinderTestCase):
    def test_each_threshold_fires_once(self):
        minder = self.make_minder({"warning_intervals": [600, 300, 60]})
        consumed = set()
        self.assertEqual(minder.consume_warning(consumed, 590), 600)
        self.assertIsNone(minder.consume_warning(consumed, 580))
        self.assertEqual(minder.consume_warning(consumed, 299), 300)

    def test_crossing_several_thresholds_warns_once(self):
        minder = self.make_minder({"warning_intervals": [600, 300, 60]})
        consumed = set()
        self.assertEqual(minder.consume_warning(consumed, 30), 600)
        self.assertIsNone(minder.consume_warning(consumed, 20))

    def test_warning_time_is_used_without_intervals(self):
        minder = self.make_minder({"warning_time": 120})
        self.assertEqual(minder.warning_intervals(), [120])


class UsageStateTest(MinderTestCase):
    def test_state_round_trips_through_file(self):
        minder = self.make_minder()
        minder.update_usage("kid", "steam*", 30)
        minder.update_group_usage("kid", "games", 40)
        minder.update_user_daily_usage("kid", 50)
        minder.save_state()

        reloaded = self.make_minder()
        self.assertEqual(reloaded.get_usage("kid", "steam*"), 30)
        self.assertEqual(reloaded.get_group_usage("kid", "games"), 40)
        self.assertEqual(reloaded.get_user_daily_usage("kid"), 50)

    def test_new_day_clears_usage(self):
        minder = self.make_minder()
        minder.update_usage("kid", "steam*", 30)
        minder.state["last_reset"] = datetime(2000, 1, 1).isoformat()
        minder.reset_daily_usage()
        self.assertEqual(minder.get_usage("kid", "steam*"), 0)

    def test_same_day_keeps_usage(self):
        minder = self.make_minder()
        minder.update_usage("kid", "steam*", 30)
        minder.reset_daily_usage()
        self.assertEqual(minder.get_usage("kid", "steam*"), 30)


class MonitorPassTest(MinderTestCase):
    """Drives monitor_processes over a faked process table"""

    def setUp(self):
        super().setUp()
        self.terminated = []

    def run_pass(self, minder, procs, elapsed=5.0):
        minder.get_username = lambda uid: {KID_UID: "kid", 0: "root"}.get(uid)
        minder.terminate_processes = lambda targets: self.terminated.extend(targets)
        minder.end_user_session = mock.Mock()
        minder.emit_warning = mock.Mock()
        with mock.patch.object(child_minder.psutil, 'process_iter', return_value=procs):
            minder.monitor_processes(elapsed)

    def terminated_pids(self):
        return sorted(proc.pid for proc, _ in self.terminated)

    def test_blocked_process_is_terminated(self):
        minder = self.make_minder({"blocked_processes": ["fortnite*"]})
        self.run_pass(minder, [FakeProc(10, "FortniteClient", KID_UID),
                               FakeProc(11, "firefox", KID_UID)])
        self.assertEqual(self.terminated_pids(), [10])

    def test_unmonitored_users_are_ignored(self):
        minder = self.make_minder({"blocked_processes": ["fortnite*"]})
        self.run_pass(minder, [FakeProc(10, "fortnite", 0)])
        self.assertEqual(self.terminated, [])

    def test_own_children_are_protected(self):
        minder = self.make_minder({"blocked_processes": ["fortnite*"]})
        self.run_pass(minder, [FakeProc(10, "fortnite", KID_UID, ppid=child_minder.os.getpid())])
        self.assertEqual(self.terminated, [])

    def test_usage_is_charged_once_per_key_per_pass(self):
        minder = self.make_minder({"limited_processes": {"chrome*": 60},
                                   "process_groups": {"browsers": ["chrome*"]}})
        procs = [FakeProc(pid, "chrome", KID_UID) for pid in range(10, 30)]
        self.run_pass(minder, procs, elapsed=5)
        self.assertEqual(minder.get_usage("kid", "chrome*"), 5)
        self.assertEqual(minder.get_group_usage("kid", "browsers"), 5)

    def test_monitored_process_is_tracked_without_limit(self):
        minder = self.make_minder({"monitored_processes": ["code"]})
        self.run_pass(minder, [FakeProc(10, "code", KID_UID)], elapsed=5)
        self.assertEqual(minder.get_usage("kid", "code"), 5)
        self.assertEqual(self.terminated, [])

    def test_process_over_limit_is_terminated(self):
        minder = self.make_minder({"limited_processes": {"minecraft*": 1}})
        minder.update_usage("kid", "minecraft*", 58)
        self.run_pass(minder, [FakeProc(10, "minecraft", KID_UID),
                               FakeProc(11, "minecraft-helper", KID_UID)])
        self.assertEqual(self.terminated_pids(), [10, 11])

    def test_process_under_limit_is_warned(self):
        minder = self.make_minder({"limited_processes": {"minecraft*": 5}})
        self.run_pass(minder, [FakeProc(10, "minecraft", KID_UID)])
        self.assertEqual(self.terminated, [])
        minder.emit_warning.assert_called_once()

    def test_group_over_limit_terminates_whole_group(self):
        minder = self.make_minder({"process_groups": {"games": ["steam*", "roblox*"]},
                                   "group_limits": {"games": 1}})
        minder.update_group_usage("kid", "games", 58)
        self.run_pass(minder, [FakeProc(10, "steam", KID_UID),
                               FakeProc(11, "robloxplayer", KID_UID),
                               FakeProc(12, "firefox", KID_UID)])
        self.assertEqual(self.terminated_pids(), [10, 11])

    def test_process_exiting_mid_pass_does_not_abort_it(self):
        minder = self.make_minder({"limited_processes": {"chrome*": 1},
                                   "user_daily_limits": {"kid": 1}})
        minder.update_usage("kid", "chrome*", 58)
        minder.update_user_daily_usage("kid", 58)
        self.run_pass(minder, [FakeProc(10, "chrome", KID_UID, vanish_on='ppid'),
                               FakeProc(11, "chrome", KID_UID)])
        self.assertEqual(self.terminated_pids(), [11])
        minder.end_user_session.assert_called_once()

    def test_daily_screen_time_limit_ends_session(self):
        minder = self.make_minder({"monitored_processes": ["code"],
                                   "user_daily_limits": {"kid": 1}})
        minder.update_user_daily_usage("kid", 58)
        self.run_pass(minder, [FakeProc(10, "code", KID_UID)])
        minder.end_user_session.assert_called_once()
        self.assertEqual(minder.end_user_session.call_args.args[0], "kid")


class UserControlTest(MinderTestCase):
    def test_expired_timed_disable_re_enables_account(self):
        self.write_user_control({"disabled_users": {"kid": {
            "re_enable_at": datetime(2000, 1, 1).isoformat()}}, "daily_schedules": {}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.run_command = mock.Mock(return_value=mock.Mock(returncode=0))
        minder.end_user_session = mock.Mock()

        minder.enforce_user_control()

        minder.run_command.assert_any_call(['usermod', '--expiredate', '', 'kid'])
        minder.end_user_session.assert_not_called()
        with open(self.user_control_path) as f:
            self.assertEqual(json.load(f)["disabled_users"], {})

    def test_disabled_account_has_session_ended(self):
        self.write_user_control({"disabled_users": {"kid": {"reason": "grounded"}},
                                 "daily_schedules": {}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.end_user_session = mock.Mock()
        minder.enforce_user_control()
        minder.end_user_session.assert_called_once()
        self.assertIn("grounded", minder.end_user_session.call_args.args[1])

    def test_user_control_disabled_in_config_does_nothing(self):
        self.write_user_control({"disabled_users": {"kid": {}}, "daily_schedules": {}})
        minder = self.make_minder()
        minder.end_user_session = mock.Mock()
        minder.enforce_user_control()
        minder.end_user_session.assert_not_called()

    def enforce_schedule_at(self, moment: datetime, monitored_users: list):
        """Enforce kid's 15-20 weekday schedule at moment, returning the end_user_session mock"""
        self.write_user_control({"disabled_users": {}, "daily_schedules": {
            "kid": {"weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": []}}})
        minder = self.make_minder({"user_control": {"enabled": True},
                                   "monitored_users": monitored_users})
        minder.end_user_session = mock.Mock()
        accounts = {"kid": mock.Mock(pw_uid=KID_UID)}
        with mock.patch.object(child_minder, 'datetime', frozen_datetime(moment)), \
                mock.patch.object(child_minder.pwd, 'getpwnam',
                                  side_effect=lambda name: accounts[name]):
            minder.enforce_user_control()
        return minder.end_user_session

    def test_listed_user_outside_hours_has_session_ended(self):
        ended = self.enforce_schedule_at(WEEKDAY.replace(hour=9), ["kid"])
        ended.assert_called_once_with("kid", "outside allowed access hours")

    def test_schedule_applies_when_every_user_is_monitored(self):
        ended = self.enforce_schedule_at(WEEKDAY.replace(hour=9), [])
        ended.assert_called_once_with("kid", "outside allowed access hours")

    def test_schedule_ignored_for_unmonitored_user(self):
        ended = self.enforce_schedule_at(WEEKDAY.replace(hour=9), ["someone_else"])
        ended.assert_not_called()

    def test_user_inside_hours_keeps_session(self):
        ended = self.enforce_schedule_at(WEEKDAY.replace(hour=16), [])
        ended.assert_not_called()

    def test_schedule_for_unknown_account_is_skipped(self):
        self.write_user_control({"disabled_users": {}, "daily_schedules": {
            "no-such-user-cmtest": {"weekday": [{"start_hour": 15, "end_hour": 20}]}}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.end_user_session = mock.Mock()
        with mock.patch.object(child_minder, 'datetime',
                               frozen_datetime(WEEKDAY.replace(hour=9))):
            minder.enforce_user_control()
        minder.end_user_session.assert_not_called()
