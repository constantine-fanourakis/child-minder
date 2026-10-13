"""Tests for usage accounting and limit decisions"""

import unittest
from datetime import datetime

from childminder import config as cfg
from childminder import policy

WEEKDAY = datetime(2026, 10, 12)


def make_config(raw: dict) -> dict:
    return cfg.validate(raw, lambda message: None, 1000)


def proc(name: str, username: str = "kid", protected: bool = False) -> policy.Process:
    return policy.Process(handle=name, name=name, username=username, protected=protected)


class LookupTest(unittest.TestCase):
    def test_first_matching_group_wins(self):
        config = make_config({"process_groups": {"games": ["steam*"], "launchers": ["steam"]}})
        self.assertEqual(policy.process_group(config, "steam"), "games")
        self.assertIsNone(policy.process_group(config, "firefox"))

    def test_process_limit_returns_config_key_and_seconds(self):
        config = make_config({"limited_processes": {"minecraft*": 60}})
        self.assertEqual(policy.process_limit(config, "minecraft-launcher"), ("minecraft*", 3600))
        self.assertEqual(policy.process_limit(config, "firefox"), (None, None))

    def test_warning_time_is_used_without_intervals(self):
        self.assertEqual(policy.warning_intervals(make_config({"warning_time": 120})), [120])
        self.assertEqual(policy.warning_intervals(make_config({"warning_intervals": [60, 600]})),
                         [600, 60])


class ConsumeWarningTest(unittest.TestCase):
    INTERVALS = [600, 300, 60]

    def test_each_threshold_fires_once(self):
        usage = policy.Usage()
        self.assertEqual(usage.consume_warning("k", 590, self.INTERVALS), 600)
        self.assertIsNone(usage.consume_warning("k", 580, self.INTERVALS))
        self.assertEqual(usage.consume_warning("k", 299, self.INTERVALS), 300)

    def test_crossing_several_thresholds_warns_once(self):
        usage = policy.Usage()
        self.assertEqual(usage.consume_warning("k", 30, self.INTERVALS), 600)
        self.assertIsNone(usage.consume_warning("k", 20, self.INTERVALS))

    def test_limits_are_tracked_separately(self):
        usage = policy.Usage()
        usage.consume_warning("a", 30, self.INTERVALS)
        self.assertEqual(usage.consume_warning("b", 30, self.INTERVALS), 600)


class UsageStateTest(unittest.TestCase):
    def test_state_round_trips(self):
        usage = policy.Usage()
        usage.charge_process("kid", "steam*", 30)
        usage.charge_group("kid", "games", 40)
        usage.charge_daily("kid", 50)
        restored = policy.Usage.from_state(usage.to_state())
        self.assertEqual(restored.process_seconds("kid", "steam*"), 30)
        self.assertEqual(restored.group_seconds("kid", "games"), 40)
        self.assertEqual(restored.daily_seconds("kid"), 50)

    def test_damaged_state_starts_empty(self):
        usage = policy.Usage.from_state({"daily_usage": [], "group_usage": {"kid": 3},
                                         "user_daily_usage": "x"})
        self.assertTrue(usage.is_empty())


class EvaluateTest(unittest.TestCase):
    def evaluate(self, raw_config, processes, usage=None, elapsed=5.0):
        self.usage = usage or policy.Usage()
        return policy.evaluate(processes, make_config(raw_config), self.usage, elapsed, WEEKDAY)

    def test_group_only_process_is_charged_to_its_group_alone(self):
        self.evaluate({"process_groups": {"media": ["mpv"]}}, [proc("mpv")])
        self.assertEqual(self.usage.groups, {"kid": {"media": 5}})
        self.assertEqual(self.usage.processes, {})

    def test_blocked_process_is_terminated_unless_protected(self):
        actions = self.evaluate({"blocked_processes": ["fortnite*"]},
                                [proc("fortnite"), proc("fortnite-helper", protected=True),
                                 proc("firefox")])
        self.assertEqual(actions.terminate, [("fortnite", "Blocked application")])

    def test_limit_charges_once_and_warns_with_the_application_name(self):
        actions = self.evaluate({"limited_processes": {"minecraft*": 5}},
                                [proc("MinecraftLauncher"), proc("minecraft-helper")])
        self.assertEqual(self.usage.process_seconds("kid", "minecraft*"), 5)
        [warning] = actions.warnings
        self.assertEqual((warning.kind, warning.subject, warning.key, warning.minutes),
                         ("process", "MinecraftLauncher", "minecraft*", 5))
        self.assertEqual(actions.terminate, [])

    def test_exhausted_group_terminates_its_processes(self):
        usage = policy.Usage()
        usage.charge_group("kid", "games", 58)
        actions = self.evaluate({"process_groups": {"games": ["steam*"]},
                                 "group_limits": {"games": 1}},
                                [proc("steam"), proc("firefox")], usage)
        self.assertEqual([handle for handle, _ in actions.terminate], ["steam"])

    def test_exhausted_daily_limit_ends_the_session(self):
        usage = policy.Usage()
        usage.charge_daily("kid", 58)
        actions = self.evaluate({"monitored_processes": ["code"],
                                 "user_daily_limits": {"kid": 1}},
                                [proc("code")], usage)
        self.assertEqual(actions.end_sessions,
                         [("kid", "daily screen time limit reached (1 minutes)")])

    def test_untracked_processes_charge_nothing(self):
        actions = self.evaluate({"user_daily_limits": {"kid": 1}}, [proc("bash")])
        self.assertTrue(self.usage.is_empty())
        self.assertEqual(actions, policy.Actions())
