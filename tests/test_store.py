"""Tests for atomic writes and the user control store"""

import fcntl
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from childminder import store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class AtomicWriteTest(StoreTestCase):
    def test_file_is_private_and_no_temp_file_remains(self):
        path = self.tmp / "data.json"
        old_umask = os.umask(0o022)
        try:
            store.atomic_write_json(path, {"a": 1})
        finally:
            os.umask(old_umask)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(os.listdir(self.tmp), ["data.json"])

    def test_failed_write_keeps_the_original(self):
        path = self.tmp / "data.json"
        store.atomic_write_json(path, {"a": 1})
        with self.assertRaises(TypeError):
            store.atomic_write_json(path, {"a": object()})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(os.listdir(self.tmp), ["data.json"])


class ReadJsonTest(StoreTestCase):
    def test_missing_file_is_none_and_bad_content_raises(self):
        path = self.tmp / "data.json"
        self.assertIsNone(store.read_json(path))
        for content in ("{not json", "[]"):
            path.write_text(content)
            with self.assertRaises(store.StoreError):
                store.read_json(path)


class NormalizeUserControlTest(unittest.TestCase):
    def test_sections_are_filled_in(self):
        self.assertEqual(store.normalize_user_control({}),
                         {"disabled_users": {}, "daily_schedules": {}})

    def test_damaged_disable_records_stay_disabled(self):
        warnings = []
        data = store.normalize_user_control({"disabled_users": {
            "kid": "yes", "kid2": {"reason": "x", "re_enable_at": "tomorrow"}}}, warnings.append)
        self.assertEqual(data["disabled_users"], {"kid": {}, "kid2": {"reason": "x"}})
        self.assertEqual(len(warnings), 1)

    def test_schedules_are_normalized(self):
        data = store.normalize_user_control(
            {"daily_schedules": {"kid": {"start_hour": 8, "end_hour": 20}}})
        self.assertEqual(data["daily_schedules"]["kid"]["weekend"],
                         [{"start_hour": 8, "end_hour": 20}])


class DisableExpiredTest(unittest.TestCase):
    def test_only_a_passed_re_enable_time_expires(self):
        now = datetime(2026, 10, 12, 12)
        self.assertTrue(store.disable_expired({"re_enable_at": "2026-10-12T11:00:00"}, now))
        self.assertFalse(store.disable_expired({"re_enable_at": "2026-10-12T13:00:00"}, now))
        self.assertFalse(store.disable_expired({}, now))

    def test_re_enable_time_with_an_offset_is_compared_as_local_time(self):
        aware = datetime(2026, 10, 12, 11, tzinfo=timezone.utc)
        local = aware.astimezone().replace(tzinfo=None)
        record = {"re_enable_at": aware.isoformat()}
        self.assertTrue(store.disable_expired(record, local + timedelta(seconds=1)))
        self.assertFalse(store.disable_expired(record, local - timedelta(seconds=1)))


class UserControlStoreTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = store.UserControlStore(self.tmp / "user_control.json")

    def test_update_saves_only_a_change(self):
        with self.store.update():
            pass
        self.assertFalse(self.store.path.exists())
        with self.store.update() as data:
            data["disabled_users"]["kid"] = {}
        self.assertEqual(self.store.load()["disabled_users"], {"kid": {}})

    def test_on_save_runs_only_after_a_save(self):
        saves = []
        with self.store.update(on_save=lambda: saves.append(self.store.path.exists())):
            pass
        self.assertEqual(saves, [])
        with self.store.update(on_save=lambda: saves.append(self.store.path.exists())) as data:
            data["disabled_users"]["kid"] = {}
        self.assertEqual(saves, [True])

    def test_update_saves_nothing_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with self.store.update() as data:
                data["disabled_users"]["kid"] = {}
                raise RuntimeError
        self.assertFalse(self.store.path.exists())

    def test_update_holds_the_lock(self):
        with self.store.update():
            fd = os.open(self.store.lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
