"""Unit tests for the child-minder daemon's interfaces"""

import fcntl
import json
import os
import socket
import unittest
from datetime import datetime
from unittest import mock

from childminder import policy, store
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


class ConfigTest(MinderTestCase):
    def test_config_is_validated_on_load(self):
        minder = self.make_minder({"check_interval": -1, "user_daily_limits": {"kid": 90}})
        self.assertEqual(minder.config["check_interval"], 5)
        self.assertEqual(minder.config["user_daily_limits"],
                         {"kid": {"weekday": 90, "weekend": 90}})

    def test_non_dict_user_control_does_not_break_enforcement(self):
        minder = self.make_minder({"user_control": None})
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

    def test_accounts_above_uid_max_are_never_monitored(self):
        minder = self.make_minder({"monitored_users": []})
        self.assertFalse(minder.should_monitor_user("nobody", 65534))
        self.assertFalse(minder.should_monitor_user("dynamic", 61184))
        self.assertTrue(minder.should_monitor_user("kid", minder.uid_max))

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
        minder.user_control_state = store.normalize_user_control(
            {"daily_schedules": {"kid": schedule}})
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

    def test_no_access_day_type_is_denied_all_day(self):
        schedule = {"weekday": [{"start_hour": 15, "end_hour": 20}], "weekend": [],
                    "no_access": ["weekend"]}
        for hour in (0, 12, 23):
            self.assertFalse(self.access_at(WEEKEND.replace(hour=hour), schedule))
        self.assertTrue(self.access_at(WEEKDAY.replace(hour=16), schedule))

    def test_no_access_overrides_stray_windows(self):
        schedule = {"weekday": [{"start_hour": 0, "end_hour": 23}], "weekend": [],
                    "no_access": ["weekday"]}
        self.assertFalse(self.access_at(WEEKDAY.replace(hour=12), schedule))

    def test_legacy_flat_schedule_applies_to_all_days(self):
        schedule = {"start_hour": 8, "end_hour": 20}
        self.assertFalse(self.access_at(WEEKDAY.replace(hour=21), schedule))
        self.assertFalse(self.access_at(WEEKEND.replace(hour=21), schedule))
        self.assertTrue(self.access_at(WEEKEND.replace(hour=10), schedule))


class UsageStateTest(MinderTestCase):
    def test_state_round_trips_through_file(self):
        minder = self.make_minder()
        minder.usage.charge_process("kid", "steam*", 30)
        minder.usage.charge_group("kid", "games", 40)
        minder.usage.charge_daily("kid", 50)
        minder.save_state()

        reloaded = self.make_minder()
        self.assertEqual(reloaded.usage.process_seconds("kid", "steam*"), 30)
        self.assertEqual(reloaded.usage.group_seconds("kid", "games"), 40)
        self.assertEqual(reloaded.usage.daily_seconds("kid"), 50)

    def test_saved_state_is_private(self):
        minder = self.make_minder()
        old_umask = os.umask(0o022)
        try:
            minder.save_state()
        finally:
            os.umask(old_umask)
        self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o600)

    def test_new_day_clears_usage(self):
        minder = self.make_minder()
        minder.usage.charge_process("kid", "steam*", 30)
        minder.state["last_reset"] = datetime(2000, 1, 1).isoformat()
        minder.reset_daily_usage()
        self.assertEqual(minder.usage.process_seconds("kid", "steam*"), 0)

    def test_badly_typed_state_values_do_not_stop_the_daemon(self):
        with open(self.state_path, 'w') as f:
            json.dump({"last_reset": 5, "daily_usage": {"kid": {"x": None}}}, f)
        minder = self.make_minder()
        minder.reset_daily_usage()
        self.assertIsNotNone(store.parse_time(minder.state["last_reset"]))
        self.assertEqual(minder.usage.processes, {"kid": {}})

    def test_same_day_keeps_usage(self):
        minder = self.make_minder()
        minder.usage.charge_process("kid", "steam*", 30)
        minder.reset_daily_usage()
        self.assertEqual(minder.usage.process_seconds("kid", "steam*"), 30)


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

    def test_bad_limit_value_does_not_stop_blocking(self):
        minder = self.make_minder({"blocked_processes": ["bad"],
                                   "process_groups": {"g": ["game"]},
                                   "group_limits": {"g": "120"}})
        self.run_pass(minder, [FakeProc(10, "game", KID_UID), FakeProc(11, "bad", KID_UID)])
        self.assertEqual(self.terminated_pids(), [11])

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
        self.assertEqual(minder.usage.process_seconds("kid", "chrome*"), 5)
        self.assertEqual(minder.usage.group_seconds("kid", "browsers"), 5)

    def test_monitored_process_is_tracked_without_limit(self):
        minder = self.make_minder({"monitored_processes": ["code"]})
        self.run_pass(minder, [FakeProc(10, "code", KID_UID)], elapsed=5)
        self.assertEqual(minder.usage.process_seconds("kid", "code"), 5)
        self.assertEqual(self.terminated, [])

    def test_process_over_limit_is_terminated(self):
        minder = self.make_minder({"limited_processes": {"minecraft*": 1}})
        minder.usage.charge_process("kid", "minecraft*", 58)
        self.run_pass(minder, [FakeProc(10, "minecraft", KID_UID),
                               FakeProc(11, "minecraft-helper", KID_UID)])
        self.assertEqual(self.terminated_pids(), [10, 11])

    def test_process_under_limit_is_warned(self):
        minder = self.make_minder({"limited_processes": {"minecraft*": 5}})
        self.run_pass(minder, [FakeProc(10, "minecraft", KID_UID)])
        self.assertEqual(self.terminated, [])
        minder.emit_warning.assert_called_once()

    def test_warning_names_the_application_not_the_pattern(self):
        minder = self.make_minder({"limited_processes": {"minecraft*": 5}})
        self.run_pass(minder, [FakeProc(10, "MinecraftLauncher", KID_UID),
                               FakeProc(11, "minecraft-helper", KID_UID)])
        message = minder.emit_warning.call_args.args[2]
        self.assertTrue(message.startswith("MinecraftLauncher has 5 minutes remaining"), message)

    def test_warning_under_a_minute_says_less_than_one_minute(self):
        minder = self.make_minder()
        minder.emit_warning = mock.Mock()
        minder.deliver_warning(policy.LimitWarning("daily", "kid", "", "", 30))
        message, span = minder.emit_warning.call_args.args[2], minder.emit_warning.call_args.args[4]
        self.assertTrue(message.startswith("You have less than 1 minute of screen time"), message)
        self.assertEqual(span, "less than 1 minute")

    def test_warning_of_exactly_a_minute_says_one_minute(self):
        minder = self.make_minder()
        minder.emit_warning = mock.Mock()
        minder.deliver_warning(policy.LimitWarning("group", "kid", "games", "games", 60))
        message = minder.emit_warning.call_args.args[2]
        self.assertTrue(message.startswith("Group 'games' has 1 minute remaining"), message)

    def test_group_over_limit_terminates_whole_group(self):
        minder = self.make_minder({"process_groups": {"games": ["steam*", "roblox*"]},
                                   "group_limits": {"games": 1}})
        minder.usage.charge_group("kid", "games", 58)
        self.run_pass(minder, [FakeProc(10, "steam", KID_UID),
                               FakeProc(11, "robloxplayer", KID_UID),
                               FakeProc(12, "firefox", KID_UID)])
        self.assertEqual(self.terminated_pids(), [10, 11])

    def test_zero_limits_allow_no_time(self):
        minder = self.make_minder({"process_groups": {"games": ["steam*"]},
                                   "group_limits": {"games": 0},
                                   "user_daily_limits": {"kid": {"weekday": 0, "weekend": 120}}})
        with mock.patch.object(child_minder, 'datetime', frozen_datetime(WEEKDAY)):
            self.run_pass(minder, [FakeProc(10, "steam", KID_UID)])
        self.assertEqual(self.terminated_pids(), [10])
        minder.end_user_session.assert_called_once()

    def test_daily_screen_time_limit_ends_session(self):
        minder = self.make_minder({"monitored_processes": ["code"],
                                   "user_daily_limits": {"kid": 1}})
        minder.usage.charge_daily("kid", 58)
        self.run_pass(minder, [FakeProc(10, "code", KID_UID)])
        minder.end_user_session.assert_called_once()
        self.assertEqual(minder.end_user_session.call_args.args[0], "kid")


class RunLoopTest(MinderTestCase):
    def run_once(self, minder):
        def sleep(seconds):
            minder.running = False
        with mock.patch.object(child_minder.time, 'sleep', side_effect=sleep):
            minder.run()

    def test_user_control_runs_while_monitoring_is_off(self):
        minder = self.make_minder({"enabled": False})
        minder.enforce_user_control = mock.Mock()
        minder.monitor_processes = mock.Mock()
        self.run_once(minder)
        minder.enforce_user_control.assert_called_once()
        minder.monitor_processes.assert_not_called()

    def test_monitoring_pass_runs_while_enabled(self):
        minder = self.make_minder()
        minder.enforce_user_control = mock.Mock()
        minder.monitor_processes = mock.Mock()
        self.run_once(minder)
        minder.monitor_processes.assert_called_once()


    def test_run_reports_ready_to_systemd(self):
        minder = self.make_minder()
        minder.enforce_user_control = mock.Mock()
        minder.monitor_processes = mock.Mock()
        path = str(self.tmp / "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind(path)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                self.run_once(minder)
            self.assertEqual(sock.recv(64), b"READY=1")


class NotifySystemdTest(unittest.TestCase):
    def test_abstract_socket(self):
        name = f"child-minder-test-{os.getpid()}"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind("\0" + name)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "@" + name}):
                child_minder.notify_systemd("READY=1")
            self.assertEqual(sock.recv(64), b"READY=1")

    def test_does_nothing_outside_systemd(self):
        with mock.patch.dict(os.environ, clear=True), \
                mock.patch.object(child_minder.socket, 'socket') as sock:
            child_minder.notify_systemd("READY=1")
        sock.assert_not_called()


class SignalTest(MinderTestCase):
    def test_stop_signal_only_sets_a_flag(self):
        minder = self.make_minder()
        with self.assertNoLogs("childminder"):
            minder.stop_handler(child_minder.signal.SIGTERM, None)
        self.assertFalse(minder.running)


class TerminalMessageTest(MinderTestCase):
    def test_message_reaches_each_terminal_once(self):
        tty = self.tmp / "tty"
        tty.write_text("")
        procs = [mock.Mock(info={'username': "kid", 'terminal': str(tty)}) for _ in range(2)]
        minder = self.make_minder()
        with mock.patch.object(child_minder.psutil, 'process_iter', return_value=procs):
            self.assertTrue(minder.write_to_user_terminals("kid", "hello"))
        self.assertEqual(tty.read_text().count("hello"), 1)


class EndSessionTest(MinderTestCase):
    def end_session(self, procs):
        minder = self.make_minder()
        minder.get_username = lambda uid: {KID_UID: "kid"}.get(uid)
        minder.run_command = mock.Mock()
        minder.send_notification = mock.Mock()
        minder.write_to_user_terminals = mock.Mock()
        minder.terminate_processes = mock.Mock()
        with mock.patch.object(child_minder.psutil, 'process_iter', return_value=procs):
            minder.end_user_session("kid", "outside allowed access hours")
        return minder

    def test_logged_out_user_is_left_alone_quietly(self):
        with self.assertNoLogs("childminder", "WARNING"):
            minder = self.end_session([FakeProc(10, "bash", 1002)])
        minder.run_command.assert_not_called()
        minder.send_notification.assert_not_called()
        minder.terminate_processes.assert_not_called()

    def test_logged_in_user_has_session_ended(self):
        with self.assertLogs("childminder", "WARNING"):
            minder = self.end_session([FakeProc(10, "bash", KID_UID),
                                       FakeProc(11, "firefox", KID_UID)])
        minder.run_command.assert_called_once_with(
            ['loginctl', 'terminate-user', 'kid'], check=False)
        minder.send_notification.assert_called_once()
        targets = minder.terminate_processes.call_args.args[0]
        self.assertEqual(sorted(p.pid for p, _ in targets), [10, 11])

    def test_repeat_within_interval_is_throttled(self):
        minder = self.end_session([FakeProc(10, "bash", KID_UID)])
        with mock.patch.object(child_minder.psutil, 'process_iter',
                               return_value=[FakeProc(10, "bash", KID_UID)]):
            minder.end_user_session("kid", "again")
        minder.run_command.assert_called_once()


class UsageSummaryTest(MinderTestCase):
    def test_limit_is_found_for_any_pattern_key(self):
        minder = self.make_minder({"limited_processes": {"[Mm]inecraft": 60}})
        minder.usage.charge_process("kid", "[Mm]inecraft", 600)
        with self.assertLogs("childminder", "INFO") as logs:
            minder.log_usage_summary()
        self.assertTrue(any("[Mm]inecraft" in line and "Remaining: 50 minutes" in line
                            for line in logs.output), logs.output)

    def test_monitored_key_has_no_remaining_time(self):
        minder = self.make_minder({"monitored_processes": ["code"]})
        minder.usage.charge_process("kid", "code", 600)
        with self.assertLogs("childminder", "INFO") as logs:
            minder.log_usage_summary()
        self.assertFalse(any("Remaining" in line for line in logs.output))


class UserControlTest(MinderTestCase):
    def setUp(self):
        super().setUp()
        accounts = {"kid": mock.Mock(pw_uid=KID_UID), "root": mock.Mock(pw_uid=0)}
        patcher = mock.patch.object(child_minder.pwd, 'getpwnam',
                                    side_effect=lambda name: accounts[name])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_expired_timed_disable_re_enables_account(self):
        self.write_user_control({"disabled_users": {"kid": {
            "re_enable_at": datetime(2000, 1, 1).isoformat()}}, "daily_schedules": {}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.run_command = mock.Mock(return_value=mock.Mock(returncode=0))
        minder.end_user_session = mock.Mock()

        minder.enforce_user_control()

        minder.run_command.assert_any_call(
            child_minder.accounts.SYSTEMD_RUN + ['usermod', '--expiredate', '', 'kid'],
            check=False)
        minder.end_user_session.assert_not_called()
        with open(self.user_control_path) as f:
            self.assertEqual(json.load(f)["disabled_users"], {})

    def test_account_is_unlocked_while_holding_the_user_control_lock(self):
        self.write_user_control({"disabled_users": {"kid": {
            "re_enable_at": datetime(2000, 1, 1).isoformat()}}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        lock_held = []

        def run_command(argv, check=True):
            fd = os.open(minder.user_control.lock_path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held.append(False)
            except BlockingIOError:
                lock_held.append(True)
            finally:
                os.close(fd)
            return mock.Mock(returncode=0)
        minder.run_command = run_command
        minder.enforce_user_control()
        self.assertEqual(lock_held, [True, True])

    def test_disable_made_after_the_check_began_is_kept(self):
        expired = {"re_enable_at": datetime(2000, 1, 1).isoformat()}
        self.write_user_control({"disabled_users": {"kid": expired}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.run_command = mock.Mock(return_value=mock.Mock(returncode=0))
        minder.end_user_session = mock.Mock()
        self.write_user_control({"disabled_users": {"kid": {"reason": "grounded again"}}})

        minder.handle_disabled_user("kid")

        minder.run_command.assert_not_called()
        with open(self.user_control_path) as f:
            self.assertEqual(json.load(f)["disabled_users"], {"kid": {"reason": "grounded again"}})
        minder.end_user_session.assert_called_once()

    def test_unreadable_file_keeps_the_last_good_copy(self):
        self.write_user_control({"disabled_users": {"kid": {"reason": "grounded"}}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        self.user_control_path.write_text("{not json")
        minder.end_user_session = mock.Mock()
        minder.enforce_user_control()
        minder.end_user_session.assert_called_once()

    def test_disabled_account_has_session_ended(self):
        self.write_user_control({"disabled_users": {"kid": {"reason": "grounded"}},
                                 "daily_schedules": {}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.end_user_session = mock.Mock()
        minder.enforce_user_control()
        minder.end_user_session.assert_called_once()
        self.assertIn("grounded", minder.end_user_session.call_args.args[1])

    def test_first_check_runs_soon_after_boot(self):
        self.write_user_control({"disabled_users": {"kid": {"reason": "grounded"}},
                                 "daily_schedules": {}})
        minder = self.make_minder({"user_control": {"enabled": True, "check_interval": 60}})
        minder.end_user_session = mock.Mock()
        with mock.patch.object(child_minder.time, 'monotonic', return_value=15.0):
            minder.enforce_user_control()
        minder.end_user_session.assert_called_once()

    def test_disabled_system_account_is_left_alone(self):
        self.write_user_control({"disabled_users": {"root": {"reason": "mistake"}},
                                 "daily_schedules": {}})
        minder = self.make_minder({"user_control": {"enabled": True}})
        minder.end_user_session = mock.Mock()
        minder.enforce_user_control()
        minder.end_user_session.assert_not_called()

    def test_timed_disable_expires_while_monitoring_is_off(self):
        self.write_user_control({"disabled_users": {
            "kid": {"re_enable_at": datetime(2000, 1, 1).isoformat()}}})
        minder = self.make_minder({"enabled": False, "user_control": {"enabled": True}})
        minder.run_command = mock.Mock(return_value=mock.Mock(returncode=0))
        minder.enforce_user_control()
        with open(self.user_control_path) as f:
            self.assertEqual(json.load(f)["disabled_users"], {})

    def test_no_session_is_ended_while_monitoring_is_off(self):
        self.write_user_control({"disabled_users": {"kid": {"reason": "grounded"}},
                                 "daily_schedules": {"kid": {"no_access": ["weekday", "weekend"]}}})
        minder = self.make_minder({"enabled": False, "user_control": {"enabled": True}})
        minder.end_user_session = mock.Mock()
        minder.enforce_user_control()
        minder.end_user_session.assert_not_called()

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
