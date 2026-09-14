"""Recovery checks use only fake launchctl and activity; never touch live jobs."""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ux46_service_control import ServiceControl, LABEL
from test_ux46_access_gateway import GateCase, ORIGIN, cookie_value

IDLE = {"state": "idle", "active_turns": 0, "pending_inputs": 0, "workers": 2}
BUSY = {"state": "busy", "active_turns": 1, "pending_inputs": 1, "workers": 2}
UNKNOWN = {"state": "unknown", "active_turns": None, "pending_inputs": None, "workers": None}


class Launchd:
    def __init__(self, *, stubborn=False, replacement=False):
        self.pid = 123
        self.stubborn, self.replacement = stubborn, replacement
        self.calls = []
        self.signalled = threading.Event()
        self.release = None

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        command = args[1]
        if command == "kill":
            self.signalled.set()
            if self.release:
                self.release.wait(2)
            if not self.stubborn:
                self.pid = 456 if self.replacement else None
        if command == "kickstart":
            self.pid = 456
        return SimpleNamespace(returncode=0, stdout=f" pid = {self.pid}\n" if self.pid else "state = waiting\n")


def control(runner=None, activity=None, **kwargs):
    return ServiceControl(SimpleNamespace(host="127.0.0.1", port=8877), None,
                          runner=runner or Launchd(), platform="darwin", uid=501, enabled=True,
                          probe=lambda: dict(activity or IDLE), **kwargs)


def finish(controller):
    deadline = time.monotonic() + 3
    while controller.status()["restart_in_progress"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return controller.status()


class Controller(unittest.TestCase):
    def test_graceful_stop_then_start_fixed_job_and_no_retry(self):
        runner = Launchd()
        c = control(runner)
        self.assertEqual(c.restart({"service": "console"})[0], 202)
        self.assertEqual(finish(c)["state"], "ready")
        mutations = [x for x in runner.calls if x[1] != "print"]
        self.assertEqual(mutations, [["/bin/launchctl", "kill", "SIGINT", f"gui/501/{LABEL}"],
                                     ["/bin/launchctl", "kickstart", f"gui/501/{LABEL}"]])
        self.assertEqual(c.restart({"service": "console"})[0], 429)

    def test_keepalive_replacement_is_not_restarted(self):
        runner = Launchd(replacement=True)
        c = control(runner)
        c.restart({"service": "console"})
        self.assertEqual(finish(c)["state"], "ready")
        self.assertEqual([x[1] for x in runner.calls if x[1] != "print"], ["kill"])

    def test_stubborn_process_reports_failure_without_force_or_kickstart(self):
        runner = Launchd(stubborn=True)
        c = control(runner, stop_timeout=0)
        c.restart({"service": "console"})
        final = finish(c)
        self.assertEqual(final["state"], "failed")
        self.assertIn("No force kill", final["message"])
        self.assertEqual([x[1] for x in runner.calls if x[1] != "print"], ["kill"])

    def test_busy_and_unknown_require_explicit_confirmation(self):
        for activity in (BUSY, UNKNOWN):
            with self.subTest(activity=activity):
                runner = Launchd()
                c = control(runner, activity=activity, start_timeout=0)
                status, body = c.restart({"service": "console"})
                self.assertEqual((status, body["error"]), (409, "confirmation_required"))
                self.assertFalse(runner.signalled.is_set())
                self.assertEqual(c.restart({"service": "console", "confirm_interrupt": True})[0], 202)
                finish(c)
                self.assertTrue(runner.signalled.is_set())

    def test_concurrent_restart_refused_while_status_remains_responsive(self):
        runner = Launchd()
        runner.release = threading.Event()
        c = control(runner)
        self.assertEqual(c.restart({"service": "console"})[0], 202)
        self.assertTrue(runner.signalled.wait(1))
        self.assertTrue(c.status()["restart_in_progress"])
        self.assertEqual(c.restart({"service": "console"})[0], 409)
        runner.release.set()
        self.assertEqual(finish(c)["state"], "ready")

    def test_arbitrary_targets_and_extra_arguments_are_refused_before_execution(self):
        runner = Launchd()
        c = control(runner)
        for payload in ({"service": "telld"}, {"service": "console", "pid": 123},
                        {"service": "console", "label": "anything"},
                        {"service": "console", "host": "remote"},
                        {"service": "console", "confirm_interrupt": "true"}, []):
            self.assertEqual(c.restart(payload)[0], 400)
        self.assertEqual(runner.calls, [])


class ServiceHTTP(GateCase):
    def setUp(self):
        super().setUp()
        self.runner = Launchd()
        self.server.service_control = control(self.runner)

    def status_login(self):
        status, headers, body = self.ask(path="/api/service/status")
        self.assertEqual(status, 200)
        return cookie_value(headers), json.loads(body)["csrf_token"]

    def restart(self, cookie, csrf, **kwargs):
        return self.ask(method="POST", path="/api/service/restart", password=None,
                        cookie=cookie, body=json.dumps({"service": "console"}),
                        headers={"Origin": kwargs.get("origin", ORIGIN),
                                 "X-UX46-Service-CSRF": csrf})

    def test_status_requires_authentication_and_never_proxies(self):
        status, _, body = self.ask(path="/api/service/status", password=None)
        self.assertEqual(status, 401)
        self.assertNotIn(b"csrf_token", body)
        cookie, token = self.status_login()
        self.assertTrue(cookie and token)
        self.assertEqual(self.console.seen, [])

    def test_cookie_origin_and_gateway_csrf_all_required(self):
        cookie, token = self.status_login()
        for held, csrf, origin in ((None, token, ORIGIN), (cookie, "bad", ORIGIN),
                                   (cookie, token, "https://evil.example"), (cookie, token, "")):
            status, _, _ = self.restart(held, csrf, origin=origin)
            self.assertIn(status, (401, 403))
        status, _, _ = self.ask(method="POST", path="/api/service/restart",
                               body='{"service":"console"}',
                               headers={"Origin": ORIGIN, "X-UX46-Service-CSRF": token})
        self.assertEqual(status, 403)  # even correct Basic cannot replace the cookie
        self.assertFalse(self.runner.signalled.is_set())
        status, _, body = self.restart(cookie, token)
        self.assertEqual(status, 202, body)
        self.assertEqual(finish(self.server.service_control)["state"], "ready")
        self.assertEqual(self.console.seen, [])

    def test_console_down_does_not_block_gateway_control(self):
        self.server.service_control = control(self.runner, activity=UNKNOWN, start_timeout=0)
        cookie, token = self.status_login()
        status, _, body = self.restart(cookie, token)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "confirmation_required")
        status, _, _ = self.ask(method="POST", path="/api/service/restart", password=None,
                               cookie=cookie, body='{"service":"console","confirm_interrupt":true}',
                               headers={"Origin": ORIGIN, "X-UX46-Service-CSRF": token})
        self.assertEqual(status, 202)
        self.assertEqual(finish(self.server.service_control)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
