"""Tests for web UI password hashing"""

import unittest

from childminder import webauth


class PasswordHashTest(unittest.TestCase):
    def test_password_verifies_against_its_hash(self):
        stored = webauth.hash_password("secret")
        self.assertTrue(webauth.verify_password("secret", stored))
        self.assertFalse(webauth.verify_password("Secret", stored))

    def test_hashes_are_salted(self):
        self.assertNotEqual(webauth.hash_password("secret")["hash"],
                            webauth.hash_password("secret")["hash"])

    def test_malformed_record_never_verifies(self):
        good = webauth.hash_password("secret")
        for stored in (None, {}, "x", dict(good, salt="zz"), dict(good, n=3),
                       dict(good, n="16384"), dict(good, n=2 ** 200)):
            self.assertFalse(webauth.verify_password("secret", stored), stored)
