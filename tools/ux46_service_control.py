"""One gateway-owned recovery operation for the local UX46 console.

No request can select a command, PID, launchd label, or remote host. The
controller signals the fixed console job gracefully, waits for it to exit,
then starts it without launchctl's force-kill flag. It never resends input.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time

LABEL = "ai.workspace.ux46-console-v1"
LAUNCHCTL = "/bin/launchctl"
UNKNOWN_ACTIVITY = {"state": "unknown", "active_turns": None,
                    "pending_inputs": None, "workers": None}


class ServiceControl:
    def __init__(self, upstream, auth, *, runner=subprocess.run,
                 platform=sys.platform, uid=None, clock=time.monotonic,
                 sleep=time.sleep, stop_timeout=30.0, start_timeout=20.0,
                 cooldown=30.0, probe=None, enabled=False):
        self.upstream, self.auth = upstream, auth
        self.runner, self.clock, self.sleep = runner, clock, sleep
        self.target = f"gui/{os.getuid() if uid is None else uid}/{LABEL}"
        self.enabled = bool(enabled) and platform == "darwin" and upstream.port == 8877
        self.stop_timeout, self.start_timeout = stop_timeout, start_timeout
        self.cooldown, self.probe = cooldown, probe or self._probe
        self._lock = threading.Lock()
        self._state = "idle"
        self._message = "Restart the local console connection service."
        self._in_progress = False
        self._last_request = float("-inf")

    def _command(self, *args):
        return self.runner([LAUNCHCTL, *args, self.target], capture_output=True,
                           text=True, timeout=3, check=False)

    def _job(self):
        result = self._command("print")
        if result.returncode:
            return {"loaded": False, "pid": None}
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        return {"loaded": True, "pid": int(match[1]) if match else None}

    def _probe(self):
        connection = http.client.HTTPConnection(self.upstream.host, self.upstream.port, timeout=1)
        try:
            headers = {"Host": self.auth.public_host or f"127.0.0.1:{self.upstream.port}"}
            if self.auth.public_enabled:
                headers[self.auth.identity_header] = self.auth.public_user
            connection.request("GET", "/api/service/activity", headers=headers)
            response = connection.getresponse()
            raw = response.read(8193)
            if response.status != 200 or len(raw) > 8192:
                return dict(UNKNOWN_ACTIVITY)
            data = json.loads(raw)
            counts = {key: data[key] for key in ("active_turns", "pending_inputs", "workers")}
            if any(type(value) is not int or value < 0 for value in counts.values()):
                return dict(UNKNOWN_ACTIVITY)
            return dict(counts, state="busy" if counts["active_turns"] or counts["pending_inputs"] else "idle")
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            return dict(UNKNOWN_ACTIVITY)
        finally:
            connection.close()

    def status(self):
        supported = self.enabled
        if supported:
            try:
                supported = self._job()["loaded"]
            except (OSError, subprocess.SubprocessError):
                supported = False
        activity = self.probe() if supported else dict(UNKNOWN_ACTIVITY)
        with self._lock:
            retry_after = int(max(0, self.cooldown - (self.clock() - self._last_request)) + 0.999)
            return {"service": "console", "supported": supported,
                    "can_restart": supported and not self._in_progress and not retry_after,
                    "state": self._state, "message": self._message if supported else
                    "Service restart is unavailable on this host.",
                    "activity": activity, "requires_confirmation": activity["state"] != "idle",
                    "restart_in_progress": self._in_progress, "retry_after": retry_after}

    def restart(self, payload):
        # Exact schema prevents silently accepting an attempted wider operation.
        if (not isinstance(payload, dict) or payload.get("service") != "console"
                or set(payload) - {"service", "confirm_interrupt"}
                or type(payload.get("confirm_interrupt", False)) is not bool):
            return 400, {"error": "invalid_service", "message": "Only the local console service can be restarted."}
        current = self.status()
        if not current["supported"]:
            return 503, dict(current, error="service_unavailable")
        with self._lock:
            if self._in_progress:
                return 409, dict(current, error="restart_in_progress")
            if self.clock() - self._last_request < self.cooldown:
                return 429, dict(current, error="restart_cooldown")
            if current["requires_confirmation"] and not payload.get("confirm_interrupt"):
                return 409, dict(current, error="confirmation_required", message=(
                    "Console-owned turns or queued input may be interrupted. Confirm to restart."
                    if current["activity"]["state"] == "busy" else
                    "The console is not reporting its activity. Confirm restart knowing its turns or queued input may be interrupted."))
            self._in_progress = True
            self._last_request = self.clock()
            self._state = "stopping"
            self._message = "Stopping the console gracefully. No input will be resent."
            accepted = dict(current, state=self._state, message=self._message,
                            can_restart=False, restart_in_progress=True)
        threading.Thread(target=self._restart, name="ux46-service-restart", daemon=True).start()
        return 202, accepted

    def _set(self, state, message):
        with self._lock:
            self._state, self._message = state, message

    def _restart(self):
        try:
            before = self._job()
            if not before["loaded"]:
                raise RuntimeError("The configured console job is no longer loaded.")
            original_pid = before["pid"]
            if original_pid:
                result = self._command("kill", "SIGINT")
                if result.returncode:
                    raise RuntimeError("The console did not accept a graceful stop request.")
                deadline = self.clock() + self.stop_timeout
                while True:
                    after = self._job()
                    if after["pid"] != original_pid:
                        break
                    if self.clock() >= deadline:
                        raise RuntimeError("The console did not stop within 30 seconds. No force kill was attempted.")
                    self.sleep(0.25)
            else:
                after = before
            self._set("starting", "Starting the console and checking its connection.")
            # KeepAlive may already have replaced the original process. Never
            # kill that replacement, and never use kickstart -k.
            if not after["pid"]:
                result = self._command("kickstart")
                if result.returncode:
                    raise RuntimeError("The console could not be started. No automatic retry was attempted.")
            deadline = self.clock() + self.start_timeout
            while True:
                if self._job()["pid"] and self.probe()["state"] != "unknown":
                    self._set("ready", "The console is responding. Reopen the conversation to inspect its state; no input was resent.")
                    break
                if self.clock() >= deadline:
                    raise RuntimeError("The console was started but has not confirmed it is responding. No automatic retry was attempted.")
                self.sleep(0.25)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            # Process output never goes into the browser; launchd can include
            # environment data in its diagnostics.
            message = str(error) if isinstance(error, RuntimeError) else "The service operation failed. No force kill or automatic retry was attempted."
            self._set("failed", message)
        finally:
            with self._lock:
                self._in_progress = False
