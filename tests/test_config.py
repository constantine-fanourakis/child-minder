"""Tests for configuration validation and lookups"""

import os
import tempfile
import unittest
from datetime import datetime

from tests.support import cfg

# 2026-10-12 is a Monday, 2026-10-17 a Saturday
WEEKDAY = datetime(2026, 10, 12)
WEEKEND = datetime(2026, 10, 17)


def validate(raw: dict, uid_min: int = 1000):
    warnings = []
    return cfg.validate(raw, warnings.append, uid_min), warnings


class ValidateTest(unittest.TestCase):
    def test_missing_keys_are_filled_with_defaults(self):
        config, warnings = validate({"blocked_processes": ["x"]})
        self.assertEqual(config["blocked_processes"], ["x"])
        self.assertEqual(config["check_interval"], 5)
        self.assertEqual(config["user_control"], {"enabled": False, "check_interval": 60})
        self.assertEqual(warnings, [])

    def test_input_is_not_modified(self):
        raw = {"user_daily_limits": {"kid": 90}}
        validate(raw)
        self.assertEqual(raw, {"user_daily_limits": {"kid": 90}})

    def test_partial_nested_dict_is_completed(self):
        config, _ = validate({"user_control": {"enabled": True}})
        self.assertEqual(config["user_control"], {"enabled": True, "check_interval": 60})

    def test_invalid_scalar_values_fall_back(self):
        config, warnings = validate({"check_interval": -1, "min_uid": "1000",
                                     "enabled": "no", "warning_time": 0})
        self.assertEqual(config["check_interval"], 5)
        self.assertEqual(config["min_uid"], 1000)
        self.assertIs(config["enabled"], True)
        self.assertEqual(config["warning_time"], 300)
        self.assertEqual(len(warnings), 4)

    def test_wrong_container_type_falls_back(self):
        config, _ = validate({"blocked_processes": "steam", "group_limits": [],
                              "user_control": None})
        self.assertEqual(config["blocked_processes"], [])
        self.assertEqual(config["group_limits"], {})
        self.assertEqual(config["user_control"], {"enabled": False, "check_interval": 60})

    def test_invalid_user_control_values_fall_back(self):
        for bad in ("60", 0, -5, None):
            config, _ = validate({"user_control": {"enabled": True, "check_interval": bad}})
            self.assertEqual(config["user_control"]["check_interval"], 60)
        config, _ = validate({"user_control": {"enabled": "yes"}})
        self.assertIs(config["user_control"]["enabled"], False)

    def test_min_uid_cannot_reach_system_accounts(self):
        for bad in (0, 999, 1.5, True):
            config, warnings = validate({"min_uid": bad})
            self.assertEqual(config["min_uid"], 1000, bad)
            self.assertEqual(len(warnings), 1)
        config, _ = validate({"min_uid": 500}, uid_min=500)
        self.assertEqual(config["min_uid"], 500)
        config, _ = validate({"min_uid": 2000})
        self.assertEqual(config["min_uid"], 2000)

    def test_non_finite_numbers_are_invalid(self):
        config, _ = validate({"warning_time": float("nan"),
                              "check_interval": float("inf"),
                              "limited_processes": {"game": float("inf"), "ok": 5},
                              "user_control": {"check_interval": float("nan")}})
        self.assertEqual(config["warning_time"], 300)
        self.assertEqual(config["check_interval"], 5)
        self.assertEqual(config["limited_processes"], {"ok": 5})
        self.assertEqual(config["user_control"]["check_interval"], 60)

    def test_unusable_monitored_users_monitors_nobody(self):
        # [] means "every regular user", so a bad value must not fall back to it
        for bad in ("kid", 7, None, [7], ["kid", 7]):
            config, warnings = validate({"monitored_users": bad})
            if bad == ["kid", 7]:
                self.assertEqual(config["monitored_users"], ["kid"])
            else:
                self.assertIsNone(config["monitored_users"], bad)
            self.assertTrue(warnings, bad)
        config, warnings = validate({"monitored_users": []})
        self.assertEqual(config["monitored_users"], [])
        self.assertEqual(warnings, [])

    def test_invalid_entries_are_dropped(self):
        config, _ = validate({
            "blocked_processes": ["fortnite*", 7],
            "monitored_processes": [None, "code"],
            "process_groups": {"games": ["steam*", {}], "bad": "roblox*"},
            "limited_processes": {"chrome*": "60", "minecraft*": 30, "x": True},
            "group_limits": {"games": "120", "video": 0},
            "user_daily_limits": {"kid": {"weekday": "60", "weekend": 120},
                                  "kid2": [60], "kid3": 90,
                                  "kid4": {"weekday": 0}},
            "warning_intervals": [300, "60"],
            "usage_log_interval": None})
        self.assertEqual(config["blocked_processes"], ["fortnite*"])
        self.assertEqual(config["monitored_processes"], ["code"])
        self.assertEqual(config["process_groups"], {"games": ["steam*"]})
        self.assertEqual(config["limited_processes"], {"minecraft*": 30})
        self.assertEqual(config["group_limits"], {"video": 0})
        self.assertEqual(config["user_daily_limits"], {
            "kid3": {"weekday": 90, "weekend": 90},
            "kid4": {"weekday": 0, "weekend": None}})
        self.assertNotIn("warning_intervals", config)
        self.assertEqual(config["usage_log_interval"], 60)


class ReadFileTest(unittest.TestCase):
    def test_non_object_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            for content in ("[]", "null", "42", "{not json"):
                with open(path, 'w') as f:
                    f.write(content)
                with self.assertRaises(cfg.ConfigError):
                    cfg.read_file(path)
            with self.assertRaises(cfg.ConfigError):
                cfg.read_file(os.path.join(tmp, "missing.json"))


class UidRangeTest(unittest.TestCase):
    def write_defs(self, text: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "login.defs")
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_range_is_read_from_login_defs(self):
        path = self.write_defs("# comment\nUID_MIN\t\t500\nUID_MAX\t\t50000\n")
        self.assertEqual(cfg.read_uid_range(path), (500, 50000))

    def test_range_defaults_when_unavailable(self):
        defaults = (cfg.DEFAULT_UID_MIN, cfg.DEFAULT_UID_MAX)
        self.assertEqual(cfg.read_uid_range("/nonexistent/login.defs"), defaults)
        self.assertEqual(cfg.read_uid_range(self.write_defs("UID_MAX garbage\n")), defaults)


class PatternTest(unittest.TestCase):
    match = staticmethod(cfg.pattern_matches)

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


class UserDailyLimitTest(unittest.TestCase):
    def test_limit_follows_day_type(self):
        config, _ = validate({"user_daily_limits": {"kid": {"weekday": 0, "weekend": 120}}})
        self.assertEqual(cfg.user_daily_limit(config, "kid", WEEKDAY), 0)
        self.assertEqual(cfg.user_daily_limit(config, "kid", WEEKEND), 120)
        self.assertIsNone(cfg.user_daily_limit(config, "other", WEEKDAY))
