"""Password hashing for the web UI, shared by cmctl web-password and the server.

The hash is stored in web.json as {"password": {"salt": hex, "hash": hex,
"n": N, "r": R, "p": P}}, so the scrypt cost can be raised without
invalidating a stored password.
"""

import hashlib
import hmac
import os

# System user the web front end runs as; its group may read web.json and
# connect to the broker socket
WEB_USER = "child-minder-web"

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32


def hash_password(password: str) -> dict:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                         p=SCRYPT_P, dklen=KEY_LENGTH)
    return {"salt": salt.hex(), "hash": key.hex(),
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}


def verify_password(password: str, stored) -> bool:
    """Whether password matches a stored hash; False for any malformed record"""
    try:
        salt = bytes.fromhex(stored["salt"])
        expected = bytes.fromhex(stored["hash"])
        key = hashlib.scrypt(password.encode(), salt=salt, n=stored["n"],
                             r=stored["r"], p=stored["p"], dklen=len(expected))
    except (TypeError, KeyError, ValueError, OverflowError):
        return False
    return hmac.compare_digest(key, expected)
