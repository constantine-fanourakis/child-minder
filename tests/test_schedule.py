"""Tests for access schedule evaluation and editing"""

import unittest
from datetime import datetime

from childminder import schedule

# 2026-10-12 is a Monday, 2026-10-17 a Saturday
WEEKDAY = datetime(2026, 10, 12)
WEEKEND = datetime(2026, 10, 17)

AFTERNOON = {"start_hour": 15, "end_hour": 20}


class NormalizeTest(unittest.TestCase):
    def test_current_format_is_kept(self):
        raw = {"weekday": [AFTERNOON], "weekend": [], "no_access": ["weekend"]}
        self.assertEqual(schedule.normalize(raw), raw)

    def test_single_window_per_day_format(self):
        raw = {"weekday": {"start_hour": 8, "end_hour": 22}}
        self.assertEqual(schedule.normalize(raw), {
            "weekday": [{"start_hour": 8, "end_hour": 22}],
            "weekend": [{"start_hour": 8, "end_hour": 22}]})

    def test_flat_format_applies_to_every_day(self):
        raw = {"start_hour": 8, "end_hour": 20, "weekday": True}
        normalized = schedule.normalize(raw)
        self.assertEqual(normalized["weekday"], [{"start_hour": 8, "end_hour": 20}])
        self.assertEqual(normalized["weekend"], [{"start_hour": 8, "end_hour": 20}])

    def test_malformed_parts_are_dropped_with_a_warning(self):
        warnings = []
        raw = {"weekday": [AFTERNOON, {"start_hour": "8", "end_hour": 9},
                           {"start_hour": 5, "end_hour": 5}, "x"],
               "weekend": "all day", "no_access": ["sunday", "weekend"]}
        self.assertEqual(schedule.normalize(raw, warnings.append), {
            "weekday": [AFTERNOON], "weekend": [], "no_access": ["weekend"]})
        self.assertEqual(len(warnings), 1)

    def test_anything_else_is_an_empty_schedule(self):
        for raw in (None, [], "x", {}):
            self.assertEqual(schedule.normalize(raw), schedule.empty())

    def test_input_is_not_shared_with_the_result(self):
        raw = {"weekday": [dict(AFTERNOON)], "weekend": []}
        schedule.normalize(raw)["weekday"].clear()
        self.assertEqual(raw["weekday"], [AFTERNOON])


class IsAllowedTest(unittest.TestCase):
    def test_window_end_is_exclusive(self):
        sched = {"weekday": [AFTERNOON], "weekend": []}
        self.assertTrue(schedule.is_allowed(sched, WEEKDAY.replace(hour=15)))
        self.assertFalse(schedule.is_allowed(sched, WEEKDAY.replace(hour=20)))

    def test_overnight_window_wraps(self):
        sched = {"weekday": [], "weekend": [{"start_hour": 22, "end_hour": 6}]}
        self.assertTrue(schedule.is_allowed(sched, WEEKEND.replace(hour=23)))
        self.assertTrue(schedule.is_allowed(sched, WEEKEND.replace(hour=5)))
        self.assertFalse(schedule.is_allowed(sched, WEEKEND.replace(hour=12)))

    def test_empty_day_type_is_unrestricted_and_denied_one_is_not(self):
        sched = {"weekday": [], "weekend": [AFTERNOON]}
        self.assertTrue(schedule.is_allowed(sched, WEEKDAY.replace(hour=3)))
        schedule.deny(sched, "weekday")
        self.assertFalse(schedule.is_allowed(sched, WEEKDAY.replace(hour=3)))


class EditTest(unittest.TestCase):
    def test_deny_and_allow_keep_no_access_minimal(self):
        sched = schedule.empty()
        schedule.deny(sched, "weekend")
        schedule.deny(sched, "weekday")
        self.assertEqual(sched["no_access"], ["weekday", "weekend"])
        schedule.allow(sched, "weekday")
        schedule.allow(sched, "weekend")
        self.assertNotIn("no_access", sched)

    def test_setting_a_window_lifts_a_deny(self):
        sched = schedule.empty()
        schedule.deny(sched, "weekday")
        schedule.set_window(sched, "weekday", AFTERNOON)
        self.assertEqual(sched, {"weekday": [AFTERNOON], "weekend": []})

    def test_empty_means_nothing_is_restricted(self):
        sched = schedule.empty()
        self.assertTrue(schedule.is_empty(sched))
        schedule.deny(sched, "weekend")
        self.assertFalse(schedule.is_empty(sched))


class DescribeTest(unittest.TestCase):
    def test_same_on_all_days(self):
        sched = {"weekday": [AFTERNOON], "weekend": [AFTERNOON]}
        self.assertEqual(schedule.describe(sched),
                         ["Access Hours: 15:00 - 20:00 (all days)"])
        self.assertEqual(schedule.describe(schedule.empty()),
                         ["Access Hours: Unrestricted (all days)"])

    def test_day_types_listed_separately(self):
        sched = {"weekday": [{"start_hour": 7, "end_hour": 8}, AFTERNOON],
                 "weekend": [], "no_access": ["weekend"]}
        self.assertEqual(schedule.describe(sched, indent="  "), [
            "  Access Hours:",
            "    Weekday (Mon-Fri):",
            "      07:00 - 08:00",
            "      15:00 - 20:00",
            "    Weekend (Sat-Sun): No access"])
