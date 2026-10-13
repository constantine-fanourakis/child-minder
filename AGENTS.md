# AGENTS.md

Linux parental-control daemon (`child-minder`) + `cmctl` CLI. No build system, package manifest, CI, linter, or formatter — plain Python 3.9+ (floor matters; don't use newer syntax/features), stdlib + `psutil` only. Don't add dependencies or `pip install` (install prefers distro `python3-psutil`; root pip breaks on PEP 668 systems).

## Layout

- `child-minder.py`, `cmctl.py`, `child-minder-web.py`, `child-minder-broker.py` — 11-line entry shims only; real code is the package beside them (found via the script's real dir, which is how the `/usr/bin/cmctl` symlinks work).
- `childminder/config.py` — schema/validation. `validate()` defines the daemon's effective config. `cmctl` validates only for display/decisions (`effective_config()`); its mutating commands deliberately edit the file as written (`load_config()`) — don't switch them to the validated form, or an unrelated edit would silently drop invalid entries, clamp `min_uid`, and canonicalize `user_daily_limits` on save.
- `childminder/policy.py` — pure usage accounting/`evaluate()`; no side effects. Daemon executes the returned actions.
- `childminder/daemon.py` — `ChildMinder`: process scanning, termination, notifications, session ending, signals.
- `childminder/cli.py` — `cmctl` (`ChildMinderManager` + argparse dispatch in `main()`).
- `childminder/schedule.py`, `store.py`, `accounts.py` — schedule semantics, on-disk state, account lock/unlock; shared by daemon and CLI, so don't fork logic into one caller.
- `childminder/web.py` — web UI front end (HTTP, login, sessions, CSRF), runs as the unprivileged `child-minder-web` user; `static/` holds the page (plain JS, no external resources, server data inserted with `textContent` only).
- `childminder/broker.py` — root side of the web UI: one process per connection on `/run/child-minder-broker.sock` (`Accept=yes`), fixed operations only. Every change is a cmctl subcommand from `broker.COMMANDS`; don't give the broker its own editing logic or let the front end read root-only files.
- `childminder/webauth.py` — scrypt password hashing shared by `cmctl web-password` and the front end.
- `childminder/x11.py` — screen capture over the X11 protocol (GetImage + stdlib PNG encoder). The broker runs it as a script under `python3 -I`, as the display's owner, so it must import only the standard library; never call it in the root process, since its DISPLAY/XAUTHORITY come from the user.
- `config.json` (repo copy is the installer default, with placeholder user `son_username`), `install.sh`, `uninstall.sh`, `child-minder.service`, `child-minder-web.service`, `child-minder-broker.socket`, `child-minder-broker@.service`, `cmctl-completion.bash`.

## Commands

```bash
python3 -m unittest                                        # full suite (a few seconds), run as regular user
python3 -m unittest tests.test_schedule.NormalizeTest.test_current_format_is_kept
tests/functional/run.sh [--from REV]                         # Docker, privileged systemd container, ~4 min — only when needed
```

- Integration tests (`tests/test_integration.py`) spawn a renamed copy of `sleep` for the current user and are skipped if UID < 1000, so a root run silently covers less.
- `test-child-minder.sh` is a post-install (root, live-system) check, not a dev test.
- No verification step beyond the tests exists; if you add tooling, wire it in rather than assuming it.

## Safety — this code runs as root and acts on real users

- Never run the daemon, mutating `cmctl`, `install.sh`, or `uninstall.sh` against the host to "test" a change. Use the unit tests, which redirect every path to a temp dir (`tests/support.py`: `MinderTestCase`, `CmctlTestCase`, `FakeProc`).
- `tests/support.py` also patches `cfg.read_uid_range` to `(1000, 60000)` — don't let host `/etc/login.defs` leak into tests.

## Invariants that are easy to break

- Process patterns: exact, case-insensitive name match unless the pattern contains `*?[`, which makes it a glob. Never substring matching (`sh` must not match `bash` — patterns decide what root kills). One counter per pattern, shared by helper processes.
- Config validation drops/warns only the bad entry in a parseable JSON object and keeps enforcing the rest. A missing, unreadable, malformed, or non-object file is rejected wholesale (`ConfigError`): daemon startup falls back to the last-good config (or exits if none), reload keeps the running config, and `cmctl` exits. `min_uid` is clamped up to `UID_MIN` from `/etc/login.defs`. A limit of `0` minutes means no time allowed, not "unset". `monitored_users`: `"*"` means every regular account, `[]` (the default, and the fallback for an invalid value) means nobody. Configs before `config_version` 2 used `[]` for everyone; `config.upgrade()` rewrites them once at install — keep it working, and bump `CONFIG_VERSION` with a new step for any future change of meaning. Use `config.monitors()` / `config.is_regular_uid()` rather than re-deriving either rule.
- File ownership (`childminder/store.py` header): `config.json` written by cmctl, read by daemon; `state.json` written by daemon (cmctl may rewrite it only while stopped); `user_control.json` written by both, always while holding the `UserControlStore` flock — normally via `update()`, but `cmctl disable-user` uses an explicit `locked()` block on purpose: it saves the disable record before locking the account (and restores the old record if locking fails), so don't refactor it into `update()`, which saves only after the body. All JSON writes go through `atomic_write_json` (0600, unique temp file).
- Installed paths are hardcoded in `daemon.py` `DEFAULT_*_PATH`, `cli.py` `*_PATH` and `broker.py` `*_PATH` constants; `web.json` in `cli.py` and `web.py`; the broker socket in `broker.py` and `child-minder-broker.socket`. Change all of them.
- Schedules: no windows = unrestricted unless the day type is in `no_access` (denied). Removing the last window **denies** the day type; clearing windows makes it unrestricted. A day type whose windows are all malformed is denied rather than left unrestricted (if at least one window parses, the malformed ones are dropped and the rest govern). When prose in `README.md`/`QUICK-REFERENCE.md` and `schedule.py` disagree, the code wins.
- Signals: SIGHUP = reload config, SIGUSR1 = reset daily usage, SIGTERM/SIGINT = stop. The unit is `Type=notify` and only reports READY after handlers are installed — a reload before that kills the daemon.
- Account locking: expiring the account (`usermod --expiredate 1`) is what blocks all logins; `passwd -l` is best-effort only. The daemon must run account changes via `systemd-run` (`accounts.SYSTEMD_RUN`) because the unit's `ProtectSystem=strict` makes `/etc` read-only; likewise `PrivateTmp=true` gives the unit its own `/tmp` without the X11 sockets, so user helpers go over the session bus in `/run/user`. Don't "simplify" either.
- Web UI: nothing in `web.py` may need root; it reaches privileged data only through the broker. The broker reads its request before checking `SO_PEERCRED` (closing a Unix socket with unread data resets it). cmctl argv from the broker always puts positional arguments after `--` (and omits a bare `--`, which argparse rejects), so user-supplied text is never parsed as an option. `disable-user`, `enable-user` and screenshots are refused for users outside `monitored_users`. `web.json` is the one data file not 0600: `cmctl web-password` makes it 0640 group `child-minder-web` when that group exists.
- Daemon protects itself, its children, and PID 1 from termination (`policy.Process.protected`); a bad/invalid config at runtime keeps the last good one (`config.last-good.json` beside the state file).

## Cross-cutting changes

- New `cmctl` subcommand: add it in `cli.py:main()` (parser + dispatch), add to `READ_ONLY_COMMANDS` if it doesn't mutate (otherwise it's root-gated), add to `cmctl-completion.bash`, cover in `tests/test_cmctl.py`, document in `README.md` + `QUICK-REFERENCE.md`. cmctl config saves auto-run `systemctl reload`.
- New module under `childminder/`: `install.sh` copies `childminder/*.py` and `childminder/static/*` and pre-compiles bytecode (service can't write under read-only `/usr`), so only its `REQUIRED_FILES` entry is needed. A new repo-root runtime file is not copied by that glob: add an explicit install/copy line to `install.sh` (and to `REQUIRED_FILES` if it is mandatory) — `REQUIRED_FILES` alone only checks presence in the working directory. `install.sh` must be run from the repo root.
- Upgrade path is tested, not assumed: `tests/functional/run.sh --from HEAD~5` installs an old revision first; keep config/schedule migrations (see legacy formats in `schedule.normalize`) working.
