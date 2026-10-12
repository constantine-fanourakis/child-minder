"""Locking and unlocking user accounts, for cmctl and the daemon.

Expiring the account is what blocks every login path; a password lock alone
leaves SSH keys and passwordless display manager sessions working, so the
password lock is applied and lifted on a best-effort basis only.

Each function takes a runner that executes an argv and returns the completed
process, or None if it could not be run. The daemon's runner goes through
systemd-run, because the service's sandbox keeps /etc read-only and usermod
rewrites the shadow database there.
"""

import subprocess
from typing import Callable, Optional, Tuple

Runner = Callable[[list], Optional[subprocess.CompletedProcess]]

SYSTEMD_RUN = ['systemd-run', '--wait', '--collect', '--quiet', '--pipe',
               '--description=child-minder account change']


def _failure(result: Optional[subprocess.CompletedProcess]) -> Optional[str]:
    if result is None:
        return "command could not be run"
    if result.returncode != 0:
        return (result.stderr or "").strip() or f"exit status {result.returncode}"
    return None


def lock(username: str, run: Runner) -> Optional[str]:
    """Expire and password-lock an account, returning an error message on failure"""
    error = _failure(run(['usermod', '--expiredate', '1', username]))
    if error:
        return error
    run(['passwd', '-l', username])
    return None


def unlock(username: str, run: Runner) -> Tuple[Optional[str], Optional[str]]:
    """Clear an account's expiry and password lock.

    Returns (error, note): error if the account is still locked, and note if
    only the password unlock failed. passwd -u refuses to unlock an account
    that would be left passwordless, which does not stop the account from
    working.
    """
    error = _failure(run(['usermod', '--expiredate', '', username]))
    if error:
        return error, None
    return None, _failure(run(['passwd', '-u', username]))
