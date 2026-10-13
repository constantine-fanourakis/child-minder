"""Tests for the web UI: the front end on a loopback port, and the broker.

The front end is tested against a broker running in-process; the Unix
socket between them has tests of its own.
"""

import base64
import fcntl
import http.client
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from childminder import broker
from childminder import config as cfg
from childminder import web
from childminder import webauth
from childminder.store import atomic_write_json
from tests.support import UID_RANGE


class WebTestCase(unittest.TestCase):
    password = "secret"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.web_config_path = self.tmp / "web.json"
        self.config_path = self.tmp / "config.json"
        self.state_path = self.tmp / "state.json"
        self.log_path = self.tmp / "minder.log"
        self.user_control_path = self.tmp / "user_control.json"
        self.write(self.config_path, {"monitored_users": ["kid"]})
        if self.password:
            self.write(self.web_config_path,
                       {"password": webauth.hash_password(self.password)})

        patcher = mock.patch.object(cfg, "read_uid_range", return_value=UID_RANGE)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(broker.subprocess, "run",
                                    return_value=mock.Mock(stdout="active\n"))
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)

        self.broker = broker.Broker(self.config_path, self.state_path, self.log_path,
                                    self.user_control_path, self.tmp / "broker.lock")
        # Through JSON, as over the socket
        self.app = web.WebApp(self.web_config_path, lambda request: json.loads(
            json.dumps(self.broker.handle(json.loads(json.dumps(request))))))
        self.server = web.Server(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = None
        self.csrf = None

    @staticmethod
    def write(path: Path, data: dict):
        atomic_write_json(path, data)

    def request(self, method: str, path: str, body=None, headers=None,
                content_type="application/json"):
        """Send a request, returning (status, headers, decoded JSON or raw bytes)"""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        all_headers = dict(headers or {})
        if self.cookie:
            all_headers["Cookie"] = self.cookie
        data = None
        if method == "POST":
            data = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
            all_headers["Content-Type"] = content_type
            if self.csrf and "X-CSRF-Token" not in all_headers:
                all_headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=data, headers=all_headers)
        response = conn.getresponse()
        raw = response.read()
        if response.getheader("Content-Type") == "application/json":
            raw = json.loads(raw)
        return response.status, response, raw

    def login(self, password=None):
        status, response, body = self.request(
            "POST", "/api/login", {"password": password or self.password})
        if status == 200:
            self.cookie = response.getheader("Set-Cookie").split(";")[0]
            self.csrf = body["csrf"]
        return status, response, body


class StaticTest(WebTestCase):
    def test_page_is_served_with_security_headers(self):
        status, response, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Child Minder</title>", body)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

    def test_only_listed_files_are_served(self):
        for path in ("/index.html", "/../web.py", "/static/app.js", "/%2e%2e/web.py"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)

    def test_script_and_style_are_served(self):
        for path, kind in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, response, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(response.getheader("Content-Type").startswith(kind))


class LoginTest(WebTestCase):
    def test_api_requires_a_session(self):
        for path in ("/api/overview", "/api/config", "/api/logs"):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "Not logged in")

    def test_session_reports_logged_out(self):
        status, _, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"authenticated": False, "csrf": None, "password_set": True})

    def test_login_sets_a_strict_http_only_cookie(self):
        status, response, body = self.login()
        self.assertEqual(status, 200)
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertTrue(body["csrf"])
        status, _, body = self.request("GET", "/api/session")
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["csrf"], self.csrf)

    def test_wrong_password_is_refused(self):
        status, response, body = self.login("wrong")
        self.assertEqual(status, 401)
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_repeated_failures_lock_the_address_out(self):
        for _ in range(web.MAX_FAILURES):
            self.assertEqual(self.login("wrong")[0], 401)
        status, response, _ = self.login()
        self.assertEqual(status, 429)
        self.assertTrue(int(response.getheader("Retry-After")) > 0)

    def test_success_clears_earlier_failures(self):
        for _ in range(web.MAX_FAILURES - 1):
            self.login("wrong")
        self.assertEqual(self.login()[0], 200)
        self.cookie = self.csrf = None
        for _ in range(web.MAX_FAILURES - 1):
            self.assertEqual(self.login("wrong")[0], 401)

    def test_login_requires_json(self):
        status, _, _ = self.request("POST", "/api/login", b"password=secret",
                                    content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)

    def test_oversized_body_is_refused(self):
        status, _, _ = self.request("POST", "/api/login",
                                    b"x" * (web.MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    def test_logout_ends_the_session(self):
        self.login()
        status, response, _ = self.request("POST", "/api/logout")
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)

    def test_post_without_csrf_token_is_refused(self):
        self.login()
        status, _, body = self.request("POST", "/api/logout",
                                       headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/api/overview")[0], 200)

    def test_new_password_ends_existing_sessions(self):
        self.login()
        self.write(self.web_config_path, {"password": webauth.hash_password("other")})
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)

    def test_expired_session_is_refused(self):
        self.login()
        with mock.patch.object(web.time, "monotonic",
                               return_value=web.time.monotonic() + web.SESSION_SECONDS + 1):
            self.assertEqual(self.request("GET", "/api/overview")[0], 401)

    def test_unreadable_web_config_refuses_sessions(self):
        self.login()
        self.web_config_path.write_text("{broken")
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)


class NoPasswordTest(WebTestCase):
    password = None

    def test_login_explains_how_to_set_a_password(self):
        status, _, body = self.login("anything")
        self.assertEqual(status, 503)
        self.assertIn("cmctl web-password", body["error"])
        self.assertFalse(self.request("GET", "/api/session")[2]["password_set"])


class OverviewTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def overview(self) -> dict:
        status, _, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 200, body)
        return body

    def user(self, name="kid") -> dict:
        return next(u for u in self.overview()["users"] if u["name"] == name)

    def test_usage_is_paired_with_limits(self):
        self.write(self.config_path, {
            "monitored_users": ["kid"],
            "limited_processes": {"minecraft*": 60},
            "process_groups": {"games": ["steam*"]},
            "group_limits": {"games": 90},
            "user_daily_limits": {"kid": 120},
        })
        self.write(self.state_path, {
            "daily_usage": {"kid": {"minecraft*": 600}},
            "group_usage": {"kid": {"games": 1200}},
            "user_daily_usage": {"kid": 1800},
        })
        kid = self.user()
        self.assertEqual(kid["daily"], {"seconds": 1800, "limit_seconds": 7200,
                                        "remaining_seconds": 5400})
        self.assertEqual(kid["processes"], [{"subject": "minecraft*", "seconds": 600,
                                             "limit_seconds": 3600,
                                             "remaining_seconds": 3000}])
        self.assertEqual(kid["groups"][0]["remaining_seconds"], 4200)

    def test_user_without_usage_shows_their_limit(self):
        self.write(self.config_path, {"monitored_users": ["kid"],
                                      "user_daily_limits": {"kid": 30}})
        self.assertEqual(self.user()["daily"], {"seconds": 0, "limit_seconds": 1800,
                                                "remaining_seconds": 1800})

    def test_disabled_user_and_schedule_are_reported(self):
        until = (datetime.now() + timedelta(hours=1)).isoformat()
        self.write(self.user_control_path, {
            "disabled_users": {"kid": {"reason": "grounded", "re_enable_at": until}},
            "daily_schedules": {"kid": {"weekday": [], "weekend": [],
                                        "no_access": ["weekday", "weekend"]}},
        })
        kid = self.user()
        self.assertEqual(kid["disabled"]["reason"], "grounded")
        self.assertFalse(kid["allowed_now"])
        self.assertEqual(kid["schedule_days"], {"weekday": "No access", "weekend": "No access"})

    def test_users_with_records_are_listed_even_if_not_monitored(self):
        self.write(self.state_path, {"user_daily_usage": {"other": 60}})
        names = [u["name"] for u in self.overview()["users"]]
        self.assertEqual(names, ["kid", "other"])

    def test_empty_monitored_users_lists_regular_accounts(self):
        self.write(self.config_path, {"monitored_users": []})
        accounts = [mock.Mock(pw_name=name, pw_uid=uid)
                    for name, uid in (("root", 0), ("kid", 1001), ("nobody", 65534))]
        with mock.patch.object(broker.pwd, "getpwall", return_value=accounts):
            names = [u["name"] for u in self.overview()["users"]]
        self.assertEqual(names, ["kid"])

    def test_invalid_monitored_users_lists_nobody(self):
        self.write(self.config_path, {"monitored_users": "kid"})
        overview = self.overview()
        self.assertEqual(overview["users"], [])
        self.assertTrue(overview["warnings"])

    def test_service_and_monitoring_state(self):
        self.write(self.config_path, {"monitored_users": ["kid"], "enabled": False})
        overview = self.overview()
        self.assertEqual(overview["service"], "active")
        self.assertFalse(overview["monitoring"])
        self.assertIn(["systemctl", "is-active", "child-minder"],
                      [c.args[0] for c in self.run_mock.call_args_list])

    def test_broken_state_is_a_warning(self):
        self.state_path.write_text("{broken")
        overview = self.overview()
        self.assertTrue(any("Invalid JSON" in w for w in overview["warnings"]))

    def test_broken_config_is_an_error(self):
        self.config_path.write_text("{broken")
        status, _, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 503)
        self.assertIn("Invalid JSON", body["error"])


class ConfigAndLogsTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_config_is_the_validated_form_with_warnings(self):
        self.write(self.config_path, {"user_daily_limits": {"kid": 60},
                                      "group_limits": {"games": "x"}})
        status, _, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["user_daily_limits"],
                         {"kid": {"weekday": 60, "weekend": 60}})
        self.assertTrue(body["warnings"])

    def test_logs_return_the_last_lines(self):
        self.log_path.write_text("".join(f"line {i}\n" for i in range(10)))
        status, _, body = self.request("GET", "/api/logs?lines=3")
        self.assertEqual(body["lines"], ["line 7", "line 8", "line 9"])

    def test_long_log_drops_the_partial_first_line(self):
        self.log_path.write_text("x" * (broker.LOG_TAIL_BYTES + 10) + "\nlast\n")
        status, _, body = self.request("GET", "/api/logs?lines=5")
        self.assertEqual(body["lines"], ["last"])

    def test_missing_log_is_an_error(self):
        status, _, body = self.request("GET", "/api/logs")
        self.assertEqual(status, 503)

    def test_line_count_must_be_a_number(self):
        self.log_path.write_text("a\n")
        self.assertEqual(self.request("GET", "/api/logs?lines=x")[0], 400)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "web.json"

    def test_defaults_when_missing(self):
        self.assertEqual(web.load_settings(self.path),
                         {"bind": web.DEFAULT_BIND, "port": web.DEFAULT_PORT})

    def test_invalid_values_fall_back_one_by_one(self):
        atomic_write_json(self.path, {"port": True, "bind": "127.0.0.1"})
        with self.assertLogs("childminder.web", "WARNING"):
            settings = web.load_settings(self.path)
        self.assertEqual(settings, {"bind": "127.0.0.1", "port": web.DEFAULT_PORT})


class BrokerRequestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        self.broker = broker.Broker(path / "config.json", path / "state.json",
                                    path / "minder.log", path / "user_control.json")

    def test_unknown_operation_is_refused(self):
        for request in ({"op": "handle"}, {"op": "__init__"}, {}, [], "overview"):
            self.assertEqual(self.broker.handle(request)["status"], 400, request)

    def test_unexpected_error_is_reported_without_details(self):
        with mock.patch.object(self.broker, "config_view", side_effect=RuntimeError("x")), \
                self.assertLogs("childminder.broker", "ERROR"):
            self.assertEqual(self.broker.handle({"op": "config"}),
                             {"status": 500, "error": "Internal error"})

    def test_log_line_count_must_be_a_number(self):
        self.assertEqual(self.broker.handle({"op": "logs", "lines": "5"})["status"], 400)


class BrokerSocketTest(unittest.TestCase):
    """The front end reaches the broker over a Unix socket"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "broker.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(self.path)
        self.listener.listen()
        self.broker = mock.Mock(handle=lambda request: {"result": request})

    def serve_one(self):
        def accept():
            conn, _ = self.listener.accept()
            with conn:
                broker.serve_connection(self.broker, conn)
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

    def test_request_and_response_cross_the_socket(self):
        self.serve_one()
        own = mock.Mock(pw_uid=os.getuid())
        with mock.patch.object(broker.pwd, "getpwnam", return_value=own):
            response = web.socket_broker(self.path)({"op": "logs", "lines": 3})
        self.assertEqual(response, {"result": {"op": "logs", "lines": 3}})

    @unittest.skipIf(os.geteuid() == 0, "root is always allowed")
    def test_other_users_are_refused(self):
        self.serve_one()
        with mock.patch.object(broker.pwd, "getpwnam", side_effect=KeyError):
            response = web.socket_broker(self.path)({"op": "logs"})
        self.assertEqual(response, {"status": 403, "error": "Not allowed"})

    def test_unreachable_broker_is_reported(self):
        with self.assertLogs("childminder.web", "WARNING"):
            response = web.socket_broker(self.path + ".missing")({"op": "logs"})
        self.assertEqual(response["status"], 503)

    def test_front_end_turns_broker_errors_into_api_errors(self):
        app = web.WebApp(Path(self.path), lambda request: {"status": 422, "error": "no"})
        with self.assertRaises(web.ApiError) as raised:
            app.ask_broker({"op": "logs"})
        self.assertEqual((raised.exception.status, str(raised.exception)), (422, "no"))
        app = web.WebApp(Path(self.path), lambda request: ["junk"])
        with self.assertRaises(web.ApiError) as raised:
            app.ask_broker({"op": "logs"})
        self.assertEqual(raised.exception.status, 502)


class CommandTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.returncodes = {}
        self.run_mock.side_effect = self.fake_run

    def fake_run(self, argv, **kwargs):
        name = argv[2]
        return mock.Mock(returncode=self.returncodes.get(name, 0),
                         stdout=f"ran {name}\n", stderr="")

    def ran(self) -> list:
        return [c.args[0][2:] for c in self.run_mock.call_args_list]

    def post(self, *commands):
        return self.request("POST", "/api/commands", {"commands": list(commands)})

    def test_commands_run_cmctl_with_arguments_after_a_separator(self):
        status, _, body = self.post({"command": "block", "args": ["-steam"]},
                                    {"command": "limit", "args": ["minecraft*", 60]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.ran(), [["block", "--", "-steam"],
                                      ["limit", "--", "minecraft*", "60"]])
        self.assertEqual(body["output"], "ran block\nran limit")
        argv = self.run_mock.call_args.args[0]
        self.assertEqual(argv[:2], [broker.sys.executable, str(broker.CMCTL)])

    def test_command_without_arguments_has_no_separator(self):
        self.post({"command": "reset"})
        self.assertEqual(self.ran(), [["reset"]])

    def test_options_are_passed_as_single_arguments(self):
        self.post({"command": "disable-user", "args": ["kid"],
                   "options": {"hours": 1.5, "reason": "-t 99"}})
        self.assertEqual(self.ran(), [["disable-user", "--hours=1.5", "--reason=-t 99",
                                       "--", "kid"]])

    def test_unset_option_is_left_out(self):
        self.post({"command": "disable-user", "args": ["kid"], "options": {"hours": None}})
        self.assertEqual(self.ran(), [["disable-user", "--", "kid"]])

    def test_failure_stops_the_batch_and_is_reported(self):
        self.returncodes["unblock"] = 1
        status, _, body = self.post({"command": "unblock", "args": ["steam"]},
                                    {"command": "block", "args": ["other"]})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "ran unblock")
        self.assertEqual(self.ran(), [["unblock", "--", "steam"]])

    def test_invalid_requests_run_nothing(self):
        for commands in (
                [{"command": "web-password"}],
                [{"command": "config"}],
                [{"command": "block"}],
                [{"command": "block", "args": ["a", "b"]}],
                [{"command": "block", "args": [""]}],
                [{"command": "block", "args": ["a\nb"]}],
                [{"command": "block", "args": ["x" * (broker.MAX_TEXT_LENGTH + 1)]}],
                [{"command": "limit", "args": ["a", "60"]}],
                [{"command": "limit", "args": ["a", True]}],
                [{"command": "limit", "args": ["a", 1.5]}],
                [{"command": "block", "args": ["a"], "options": {"hours": 1}}],
                [{"command": "disable-user", "args": ["kid"], "options": {"hours": "1"}}],
                [{"command": "block", "args": ["a"]}, {"command": "nope"}],
                [], "block", [{"command": "reset"}] * (broker.MAX_BATCH + 1)):
            status, _, _ = self.request("POST", "/api/commands", {"commands": commands})
            self.assertEqual(status, 400, commands)
        self.assertEqual(self.ran(), [])

    def test_commands_run_while_holding_the_command_lock(self):
        held = []

        def fake_run(argv, **kwargs):
            fd = os.open(self.tmp / "broker.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.run_mock.side_effect = fake_run
        self.post({"command": "reset"}, {"command": "enable"})
        self.assertEqual(held, [True, True])

    def test_account_commands_are_limited_to_monitored_users(self):
        for command in ("disable-user", "enable-user"):
            status, _, body = self.post({"command": command, "args": ["parent"]})
            self.assertEqual(status, 403, command)
            self.assertIn("not a monitored user", body["error"])
        self.assertEqual(self.post({"command": "block", "args": ["x"]},
                                   {"command": "enable-user", "args": ["parent"]})[0], 403)
        self.assertEqual(self.ran(), [])

    def test_account_commands_follow_the_validated_monitored_users(self):
        self.write(self.config_path, {"monitored_users": []})
        self.assertEqual(self.post({"command": "enable-user", "args": ["parent"]})[0], 200)
        self.write(self.config_path, {"monitored_users": "kid"})
        self.assertEqual(self.post({"command": "enable-user", "args": ["kid"]})[0], 403)

    def test_commands_need_the_csrf_token(self):
        status, _, _ = self.request("POST", "/api/commands",
                                    {"commands": [{"command": "reset"}]},
                                    headers={"X-CSRF-Token": ""})
        self.assertEqual(status, 403)
        self.assertEqual(self.ran(), [])

    def test_commands_need_a_session(self):
        self.cookie = None
        self.assertEqual(self.post({"command": "reset"})[0], 401)
        self.assertEqual(self.ran(), [])

    def test_screenshot_is_forwarded_with_the_client_address(self):
        requests = []
        self.app.broker = lambda request: requests.append(request) or {"result": {"png": ""}}
        status, _, _ = self.request("POST", "/api/screenshot", {"user": "kid"})
        self.assertEqual(status, 200)
        self.assertEqual(requests, [{"op": "screenshot", "user": "kid", "client": "127.0.0.1"}])
        self.csrf = "wrong"
        self.assertEqual(self.request("POST", "/api/screenshot", {"user": "kid"})[0], 403)

    def test_unrunnable_cmctl_is_an_error(self):
        self.run_mock.side_effect = FileNotFoundError("missing")
        status, _, body = self.post({"command": "reset"})
        self.assertEqual(status, 500)
        self.assertIn("could not run cmctl", body["error"])


@unittest.skipIf(os.geteuid() == 0, "as root cmctl would change the installed files")
class CommandArgvTest(unittest.TestCase):
    """The argv built for each command is one the real cmctl parses.

    As a regular user cmctl parses the arguments, then stops at its root
    check (exit 1) without touching anything; a parse error exits 2.
    """

    SAMPLES = {"text": "-name", "int": 5}

    def test_every_command_parses(self):
        for name, kinds in broker.COMMANDS.items():
            request = {"command": name, "args": [self.SAMPLES[k] for k in kinds]}
            if name == "disable-user":
                request["options"] = {"hours": 1.5, "reason": "-r x"}
            result = subprocess.run(broker.command_argv(request), capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 1, (name, result.stdout, result.stderr))
            self.assertIn("must be run as root", result.stdout, name)


class ScreenshotTest(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\nimage"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        atomic_write_json(path / "config.json", {"monitored_users": ["kid"]})
        self.broker = broker.Broker(path / "config.json", path / "state.json",
                                    path / "minder.log", path / "user_control.json",
                                    path / "broker.lock")
        self.kid = mock.Mock(pw_uid=1001, pw_gid=1001, pw_dir="/home/kid")
        for target, name, kwargs in (
                (cfg, "read_uid_range", {"return_value": UID_RANGE}),
                (broker.pwd, "getpwnam", {"return_value": self.kid}),
                (broker, "find_x_session",
                 {"return_value": (":0", "/home/kid/.Xauthority")}),
                (broker.subprocess, "run",
                 {"return_value": mock.Mock(returncode=0, stdout=self.PNG, stderr=b"")})):
            patcher = mock.patch.object(target, name, **kwargs)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, f"{name}_mock", mocked)

    def shoot(self, user="kid") -> dict:
        return self.broker.handle({"op": "screenshot", "user": user, "client": "10.0.0.2"})

    def test_capture_runs_as_the_user_with_a_fixed_environment(self):
        with self.assertLogs("childminder.broker", "INFO") as logs:
            response = self.shoot()
        self.assertEqual(base64.b64decode(response["result"]["png"]), self.PNG)
        self.assertEqual(response["result"]["display"], ":0")
        argv = self.run_mock.call_args.args[0]
        kwargs = self.run_mock.call_args.kwargs
        self.assertEqual(argv, [broker.sys.executable, "-I", str(broker.X11_CAPTURE),
                                ":0", "/home/kid/.Xauthority"])
        self.assertTrue(broker.X11_CAPTURE.is_file())
        self.assertEqual((kwargs["user"], kwargs["group"], kwargs["extra_groups"]),
                         (1001, 1001, []))
        self.assertEqual(kwargs["env"]["HOME"], "/home/kid")
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertIn("10.0.0.2 took a screenshot of kid", "\n".join(logs.output))

    def test_refusals(self):
        cases = (
            ("parent", None, 403),
            ("", None, 400),
            ("kid", lambda: setattr(self.getpwnam_mock, "side_effect", KeyError), 404),
            ("kid", lambda: setattr(self.kid, "pw_uid", 0), 403),
            ("kid", lambda: setattr(self.find_x_session_mock, "return_value", None), 404),
        )
        for user, arrange, status in cases:
            with self.subTest(user=user, status=status):
                self.setUp()
                if arrange:
                    arrange()
                self.assertEqual(self.shoot(user)["status"], status)
                self.run_mock.assert_not_called()

    def test_failed_capture_is_reported(self):
        self.run_mock.return_value = mock.Mock(returncode=1, stdout=b"",
                                               stderr=b"Cannot connect to X display :0\n")
        response = self.shoot()
        self.assertEqual(response["status"], 502)
        self.assertEqual(response["error"], "Screenshot failed: Cannot connect to X display :0")

    def test_output_that_is_not_a_png_is_refused(self):
        self.run_mock.return_value = mock.Mock(returncode=0, stdout=b"GIF89a", stderr=b"")
        self.assertEqual(self.shoot()["status"], 502)


class FindXSessionTest(unittest.TestCase):
    def processes(self, *entries):
        procs = [mock.Mock(info={"uids": mock.Mock(real=uid), "environ": env})
                 for uid, env in entries]
        return mock.patch.object(broker.psutil, "process_iter", return_value=procs)

    def test_display_and_xauthority_come_from_the_users_process(self):
        with self.processes((1002, {"DISPLAY": ":1"}),
                            (1001, {}),
                            (1001, {"DISPLAY": ":0", "XAUTHORITY": "/run/user/1001/xauth"})):
            self.assertEqual(broker.find_x_session(1001, "/home/kid"),
                             (":0", "/run/user/1001/xauth"))

    def test_missing_or_odd_xauthority_falls_back_to_home(self):
        for xauthority in (None, "relative", "/tmp/x\nauth"):
            env = {"DISPLAY": ":0"}
            if xauthority:
                env["XAUTHORITY"] = xauthority
            with self.processes((1001, env)):
                self.assertEqual(broker.find_x_session(1001, "/home/kid"),
                                 (":0", "/home/kid/.Xauthority"))

    def test_invalid_or_remote_display_is_skipped(self):
        with self.processes((1001, {"DISPLAY": ":0; x"}), (1001, {"DISPLAY": "wayland"}),
                            (1001, {"DISPLAY": "host:0"})):
            self.assertIsNone(broker.find_x_session(1001, "/home/kid"))
