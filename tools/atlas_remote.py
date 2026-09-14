#!/usr/bin/env python3
"""Configured agents and the scoped proxy that reaches the remote ones.

UX46 already speaks to one native runtime on this machine. This module adds a
*server-owned* registry of agents and a narrow proxy so the same console can
also read and drive an agent that lives in another account on another host,
without giving the browser any new power.

Three boundaries hold the design together:

* **The browser never names a host, a port, a path, an executable or a
  credential.** It names an agent id from a list this server published. Local
  trusted configuration — not a request — chooses the SSH target, port and
  identity file behind that id.
* **Native isolation stays where the native runtime is.** Each remote agent
  runs its own copy of the console service on its own account-local loopback
  port, so ownership probes, profile checks and path checks are answered by the
  host that actually owns those files. UX46 forwards HTTP; it does not
  reimplement any of that here.
* **Only the console's own API surface is forwarded, operation by operation.**
  There is no "fetch this URL" endpoint. A request must match one of the
  allowlisted method/path shapes below or it is refused before any connection
  is made.

An unreachable remote agent is reported as unavailable. It is never quietly
answered by the local agent.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlencode

DEFAULT_LOCAL_AGENT = "local"
CONFIG_SCHEMA_VERSION = 1

AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
SSH_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
LABEL_RE = re.compile(r"^[^\x00-\x1f]{1,48}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# A forwarded query string is bounded and may only carry the characters the
# console's own client puts there. The remote still validates every parameter.
QUERY_RE = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/?-]{0,512}$")

MAX_PROXY_BODY = 24 * 1024 * 1024      # covers one bounded upload or download
CONNECT_TIMEOUT_S = 8.0
REQUEST_TIMEOUT_S = 30.0
CONTROL_TIMEOUT_S = 120.0
LONG_POLL_TIMEOUT_S = 45.0
TUNNEL_READY_TIMEOUT_S = 12.0
AVAILABILITY_TTL_S = 15.0

# Artifact URLs the remote minted for its own origin. They are rewritten to
# this console's origin so the browser can load a remote thumbnail, download or
# clip without ever learning the remote exists as a separate host.
ARTIFACT_URL_RE = re.compile(
    r"^/api/(?:atlas/files/[A-Za-z0-9_-]{1,128}/(?:preview|download)"
    r"|audio/[0-9a-f]{32,64}\.wav)$"
)

_ROOM = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
_ROOM_ACTIONS = ("history|search|continue|attach|submit|draft|interrupt|release|detach"
                 "|refresh|speak|pref|command|pending")

#: Every operation this proxy will forward, as ``(methods, path pattern)``.
#: The pattern must match the whole API suffix. Anything absent here — most
#: pointedly ``/api/test-thread`` — is refused with ``forbidden_path``.
PROXY_ALLOWLIST: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(methods.split()), re.compile(pattern))
    for methods, pattern in (
        ("GET", r"/api/bootstrap"),
        ("GET", r"/api/projects"),
        ("GET", r"/api/session-options"),
        ("GET", r"/api/usage"),
        ("GET", rf"/api/room/{_ROOM}/(?:usage|chapter-preview)"),
        ("POST", r"/api/sessions"),
        ("GET PATCH", r"/api/projects/[A-Za-z0-9._-]{1,64}/pref"),
        ("GET", r"/api/rooms"),
        ("GET", r"/api/workspace"),
        ("GET", r"/api/attention"),
        ("GET", r"/api/events"),
        ("GET", r"/api/approvals"),
        ("POST", r"/api/approvals/answer"),
        ("GET", r"/api/submissions"),
        ("GET", r"/api/submissions/[A-Za-z0-9_-]{8,64}"),
        ("GET", r"/api/atlas/files/[A-Za-z0-9_-]{1,128}/(?:preview|download)"),
        ("GET", r"/api/audio/[0-9a-f]{32,64}\.wav"),
        ("POST", r"/api/connection/refresh"),
        ("GET POST", rf"/api/room/{_ROOM}(?:/(?:{_ROOM_ACTIONS}))?"),
        ("PUT", rf"/api/room/{_ROOM}/draft"),
        ("POST", rf"/api/room/{_ROOM}/files"),
        ("POST", rf"/api/room/{_ROOM}/item/[A-Za-z0-9_-]{{1,128}}/attachments"),
        ("PATCH DELETE", rf"/api/room/{_ROOM}/pending/[A-Za-z0-9_-]{{8,64}}"),
    )
)

#: Response headers worth carrying back. Everything else — including the
#: remote's own security headers and any cookie it might ever grow — is dropped
#: so this console's headers remain the only ones the browser sees.
FORWARDED_RESPONSE_HEADERS = ("Content-Disposition",)

#: Request headers worth carrying forward. Authorization, cookies and the outer
#: CSRF token are deliberately absent: the remote gets its own token, obtained
#: internally, and never sees this console's.
FORWARDED_REQUEST_HEADERS = ("Content-Type", "Accept")


class RemoteError(Exception):
    """The named agent could not answer. Never a reason to answer locally."""

    def __init__(self, message: str, *, code: str = "agent_unavailable", detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


class ProxiedResponse:
    __slots__ = ("status", "body", "content_type", "headers")

    def __init__(self, status: int, body: bytes, content_type: str,
                 headers: tuple[tuple[str, str], ...] = ()):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers


# ---------------------------------------------------------------------------
# the SSH tunnel
# ---------------------------------------------------------------------------

def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SshTunnel:
    """One reused ``ssh -N -L`` forward to an account-local loopback port.

    The command is built from validated configuration as a fixed argument
    vector — there is no shell, and no part of it comes from a request. Only
    the forward is opened: no remote command runs, and nothing is written on
    the far side.
    """

    def __init__(self, *, host: str, user: str = "", ssh_port: int = 0,
                 identity_file: str = "", remote_port: int = 0, remote_socket: str = "",
                 local_port: int = 0, ssh_binary: str = "ssh"):
        if not SSH_HOST_RE.match(host or ""):
            raise ValueError("ssh host must be a plain hostname or ssh alias")
        if user and not SSH_USER_RE.match(user):
            raise ValueError("ssh user must be a plain account name")
        if ssh_port and not 1 <= int(ssh_port) <= 65535:
            raise ValueError("ssh port must be a port number")
        if not 1 <= int(remote_port) <= 65535:
            raise ValueError("remote port must be a port number")
        if local_port and not 1 <= int(local_port) <= 65535:
            raise ValueError("local port must be a port number")
        self.host = host
        self.user = user
        self.ssh_port = int(ssh_port or 0)
        if remote_socket and (not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_socket)
                              or len(remote_socket.encode()) > 100
                              or ".." in remote_socket.split("/")):
            raise ValueError("remote_socket must be a short absolute account-private socket path")
        self.remote_socket = remote_socket
        self.remote_port = int(remote_port)
        self.configured_local_port = int(local_port or 0)
        self.ssh_binary = ssh_binary
        self.identity_file = ""
        if identity_file:
            resolved = Path(identity_file).expanduser()
            # Existence is checked here so a missing key is an honest
            # "unavailable" instead of a hung connection. The file is never
            # opened, read or logged.
            if not resolved.is_file():
                raise ValueError("the configured ssh identity file does not exist")
            self.identity_file = str(resolved)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._port = 0

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _argv(self, local_port: int) -> list[str]:
        argv = [self.ssh_binary,
                "-o", "BatchMode=yes",
                "-o", "ExitOnForwardFailure=yes",
                "-o", f"ConnectTimeout={int(CONNECT_TIMEOUT_S)}",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3",
                "-N", "-T"]
        if self.identity_file:
            argv += ["-i", self.identity_file]
        if self.ssh_port:
            argv += ["-p", str(self.ssh_port)]
        destination = self.remote_socket or f"127.0.0.1:{self.remote_port}"
        argv += ["-L", f"127.0.0.1:{local_port}:{destination}", self.target]
        return argv

    def _alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None and self._port)

    def port(self) -> int:
        """The live local port, opening or re-opening the forward as needed."""

        with self._lock:
            if self._alive():
                return self._port
            self._stop_locked()
            local_port = self.configured_local_port or _free_local_port()
            try:
                proc = subprocess.Popen(
                    self._argv(local_port),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    env=dict(os.environ, SSH_ASKPASS_REQUIRE="never"),
                )
            except OSError as exc:
                raise RemoteError(f"could not start the ssh forward: {exc}",
                                  code="tunnel_failed") from exc
            self._proc = proc
            deadline = time.monotonic() + TUNNEL_READY_TIMEOUT_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    detail = self._drain_stderr()
                    self._proc = None
                    raise RemoteError(
                        "the ssh forward to this agent's host closed immediately"
                        + (f": {detail}" if detail else ""),
                        code="tunnel_failed")
                try:
                    with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
                        self._port = local_port
                        return local_port
                except OSError:
                    time.sleep(0.15)
            self._stop_locked()
            raise RemoteError("the ssh forward to this agent's host did not come up",
                              code="tunnel_failed")

    def _drain_stderr(self) -> str:
        """A short, bounded diagnostic. ssh never prints a key here."""

        proc = self._proc
        if not proc or not proc.stderr:
            return ""
        try:
            raw = proc.stderr.read() or b""
        except (OSError, ValueError):
            return ""
        return raw.decode("utf-8", "replace").strip().replace("\n", " ")[:200]

    def mark_broken(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc, self._port = self._proc, None, 0
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            with_kill = getattr(proc, "kill", None)
            if with_kill:
                try:
                    with_kill()
                except OSError:
                    pass
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def close(self) -> None:
        self.mark_broken()


class DirectPort:
    """A loopback port that is already reachable on this machine.

    Used by an agent whose console (or compatible gateway) an operator already
    runs or forwards here, and by the tests. It opens nothing and closes
    nothing; the port comes from trusted configuration, never from a request.
    """

    def __init__(self, port: int):
        self._port = int(port)

    def port(self) -> int:
        return self._port

    def mark_broken(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# one remote console
# ---------------------------------------------------------------------------

class RemoteConsole:
    """HTTP to one remote console over its loopback port.

    The remote applies its own auth boundary exactly as it does for a browser
    on its own machine: the request arrives on its loopback with its own
    ``Host``, and a mutation carries the CSRF token this class fetched from the
    remote's own ``/api/bootstrap``. That token lives in memory, is never
    logged, and is never handed to the browser.
    """

    def __init__(self, transport, remote_port: int):
        self.transport = transport
        self.remote_port = int(remote_port)
        self._csrf = ""
        self._csrf_lock = threading.Lock()

    @property
    def host_header(self) -> str:
        return f"127.0.0.1:{self.remote_port}"

    def _open(self, timeout: float) -> HTTPConnection:
        return HTTPConnection("127.0.0.1", self.transport.port(), timeout=timeout)

    def _raw(self, method: str, url: str, headers: dict, body: bytes | None,
             timeout: float) -> tuple[int, dict, bytes]:
        head = dict(headers)
        head["Host"] = self.host_header
        head["Connection"] = "close"
        conn = self._open(timeout)
        try:
            conn.request(method, url, body=body, headers=head)
            response = conn.getresponse()
            payload = response.read(MAX_PROXY_BODY + 1)
            if len(payload) > MAX_PROXY_BODY:
                raise RemoteError("that agent's answer was larger than this console forwards",
                                  code="agent_response_too_large")
            return response.status, dict(response.getheaders()), payload
        except TimeoutError as exc:
            # A native resume can outlive the HTTP response deadline. Keep the
            # shared tunnel: other conversations may still be using it, and the
            # remote operation may complete. Never replay a timed-out write.
            raise RemoteError(
                "The agent has not confirmed this request yet. Check the session's "
                "connection before retrying; the operation may still complete.",
                code="agent_request_timeout") from exc
        except (OSError, HTTPException) as exc:
            self.transport.mark_broken()
            raise RemoteError(
                "this agent's console did not answer on its own loopback port "
                f"(it may not be running there): {exc}",
                code="agent_unreachable") from exc
        finally:
            conn.close()

    def bootstrap(self) -> dict:
        """One read-only call. It resumes nothing and takes no native writer."""

        status, _headers, body = self._raw(
            "GET", "/api/bootstrap", {"Accept": "application/json"}, None, REQUEST_TIMEOUT_S)
        if status != 200:
            raise RemoteError("this agent's console refused a read of its own boot state",
                              code="agent_denied")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteError("this agent answered something that is not an UX46 console",
                              code="agent_protocol") from exc
        if not isinstance(payload, dict) or "csrf" not in payload:
            raise RemoteError("this agent answered something that is not an UX46 console",
                              code="agent_protocol")
        with self._csrf_lock:
            self._csrf = str(payload.get("csrf") or "")
        # The remote's token must not travel any further than this object.
        return {key: value for key, value in payload.items() if key != "csrf"}

    def _token(self, refresh: bool = False) -> str:
        with self._csrf_lock:
            token = self._csrf
        if token and not refresh:
            return token
        self.bootstrap()
        with self._csrf_lock:
            return self._csrf

    def request(self, method: str, url: str, *, headers: dict, body: bytes | None,
                timeout: float) -> tuple[int, dict, bytes]:
        head = dict(headers)
        head.setdefault("Accept", "application/json")
        mutating = method in ("POST", "PUT", "PATCH", "DELETE")
        if mutating:
            head["Origin"] = f"http://{self.host_header}"
            head["X-Atlas-CSRF"] = self._token()
        status, response_headers, payload = self._raw(method, url, head, body, timeout)
        if mutating and status == 403 and _error_code(payload) == "bad_csrf":
            # The remote console restarted and minted a new token. Fetch it
            # once and retry the same request; a second refusal is reported.
            head["X-Atlas-CSRF"] = self._token(refresh=True)
            status, response_headers, payload = self._raw(method, url, head, body, timeout)
        return status, response_headers, payload

    def message_for_speech(self, room: str, item_id: str) -> dict:
        """Read an exact remote message; no browser-supplied speech text."""
        cursor = ""
        seen = set()
        for _ in range(20):
            query = {"limit": 200, "direction": "desc"}
            if cursor:
                query["cursor"] = cursor
            elif "codex" == getattr(self, "runtime", ""):
                query["around"] = item_id
            path = f"/api/room/{room}/history"
            encoded = urlencode(query)
            check_allowed("GET", path, encoded)
            status, _headers, raw = self.request("GET", path + "?" + encoded,
                                                headers={}, body=None, timeout=REQUEST_TIMEOUT_S)
            if status != 200:
                raise RemoteError("The remote message could not be read", code="history_unavailable")
            payload = json.loads(raw)
            if payload.get("unavailable"):
                raise RemoteError("The remote history is unavailable", code="history_unavailable")
            found = next((item for item in payload.get("items", [])
                          if str(item.get("id", "")) == item_id), None)
            if found is not None:
                return found
            cursor = str(payload.get("next_cursor") or "")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        raise RemoteError("That message was not found in the available remote history", code="item_unknown")

    def close(self) -> None:
        self.transport.close()


def _error_code(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(parsed.get("error") or "") if isinstance(parsed, dict) else ""


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

class Agent:
    """One configured agent identity.

    ``runtime`` and ``node`` are the agent's own identity and are shown to the
    person. Transport — host, account, port, key — stays on this side and is
    never part of what the browser is told.
    """

    def __init__(self, *, agent_id: str, label: str, kind: str, runtime: str,
                 node: str, description: str = "", console: RemoteConsole | None = None,
                 transport_kind: str = "local", capabilities: tuple[str, ...] = ()):
        self.id = agent_id
        self.label = label
        self.kind = kind                 # "local" | "remote"
        self.runtime = runtime
        self.node = node
        self.description = description
        self.console = console
        if console is not None:
            console.runtime = runtime
        self.transport_kind = transport_kind
        self.capabilities = capabilities
        self.configured_capabilities = capabilities
        self._availability = {"state": "unknown", "detail": "", "checked_at": 0.0}
        self._lock = threading.Lock()

    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def note_available(self) -> None:
        with self._lock:
            self._availability = {"state": "available", "detail": "", "checked_at": time.time()}

    def note_unavailable(self, detail: str) -> None:
        with self._lock:
            self._availability = {"state": "unavailable", "detail": detail[:200],
                                  "checked_at": time.time()}

    def availability(self) -> dict:
        if self.is_local:
            return {"state": "available", "detail": "", "checked_at": time.time()}
        with self._lock:
            return dict(self._availability)

    def check(self, *, max_age: float = AVAILABILITY_TTL_S) -> dict:
        """Read-only reachability. It opens a forward and reads boot state only."""

        if self.is_local or self.console is None:
            return self.availability()
        with self._lock:
            cached = dict(self._availability)
        if cached["state"] != "unknown" and time.time() - cached["checked_at"] < max_age:
            return cached
        try:
            boot = self.console.bootstrap()
            self.capabilities = self.configured_capabilities
            declared = boot.get("capabilities")
            if isinstance(declared, dict):
                aliases = {"files": "attachments", "attach": "attach", "send": "send", "read": "read"}
                self.capabilities = tuple(c for c in self.capabilities
                                          if declared.get(aliases.get(c, c), True) is not False)
            if not (boot.get("voice") or {}).get("enabled"):
                self.capabilities = tuple(c for c in self.capabilities if c != "voice")
        except RemoteError as exc:
            self.note_unavailable(str(exc))
        else:
            self.note_available()
        return self.availability()

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "runtime": self.runtime,
            "node": self.node,
            "description": self.description,
            "transport": self.transport_kind,
            "capabilities": list(self.capabilities),
            "availability": self.availability(),
        }


DEFAULT_CAPABILITIES = ("read", "attach", "send", "files", "voice")

#: Shipped defaults. They describe identities, not permissions: an agent whose
#: console is not installed and reachable simply reports unavailable.
DEFAULT_REMOTE_AGENTS = ()


class AgentRegistry:
    """The server's own list of agents, plus the scoped proxy to remote ones."""

    def __init__(self, *, local_label: str = "Local", local_runtime: str = "codex",
                 local_node: str = "", config_path: str = "",
                 transport_factory=None, capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES):
        self.errors: list[str] = []
        self._transport_factory = transport_factory or self._transport
        self.local_id = DEFAULT_LOCAL_AGENT
        self.agents: dict[str, Agent] = {}
        definitions = self._load(config_path)
        self.agents[self.local_id] = Agent(
            agent_id=self.local_id, label=local_label, kind="local",
            runtime=local_runtime, node=local_node,
            description="This machine's own native runtime",
            transport_kind="in-process", capabilities=capabilities)
        for definition in definitions:
            agent = self._build(definition, capabilities)
            if agent is not None:
                self.agents[agent.id] = agent

    # -- configuration -----------------------------------------------------
    def _load(self, config_path: str) -> list[dict]:
        if not config_path:
            return list(DEFAULT_REMOTE_AGENTS)
        path = Path(config_path).expanduser()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.errors.append(f"{path}: no agent configuration, using the shipped defaults")
            return list(DEFAULT_REMOTE_AGENTS)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A broken trusted file must not silently fall back to a wider set.
            self.errors.append(f"{path}: unreadable agent configuration ({exc}); "
                               "only the local agent is configured")
            return []
        if not isinstance(raw, dict) or raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
            self.errors.append(f"{path}: agent configuration must be schema_version "
                               f"{CONFIG_SCHEMA_VERSION}; only the local agent is configured")
            return []
        agents = raw.get("agents")
        if not isinstance(agents, list):
            self.errors.append(f"{path}: agent configuration has no agents list")
            return []
        return [item for item in agents if isinstance(item, dict)]

    def _transport(self, definition: dict):
        kind = str(definition.get("transport") or "ssh-loopback")
        if kind == "loopback":
            # Already reachable here: an operator-managed forward, or a
            # compatible console/gateway running on this machine.
            return DirectPort(int(definition.get("remote_port") or 0))
        if kind != "ssh-loopback":
            raise ValueError(f"{kind!r} is not a supported agent transport")
        return SshTunnel(
            host=str(definition.get("ssh_host") or ""),
            user=str(definition.get("ssh_user") or ""),
            ssh_port=int(definition.get("ssh_port") or 0),
            identity_file=str(definition.get("identity_file") or ""),
            remote_port=int(definition.get("remote_port") or 0),
            remote_socket=str(definition.get("remote_socket") or ""),
            local_port=int(definition.get("local_port") or 0),
        )

    def _build(self, definition: dict, capabilities: tuple[str, ...]) -> Agent | None:
        agent_id = str(definition.get("id") or "")
        if not AGENT_ID_RE.match(agent_id):
            self.errors.append("an agent id must be a short lower-case name")
            return None
        if agent_id == self.local_id:
            self.errors.append(f"{agent_id}: the local agent id is reserved")
            return None
        label = str(definition.get("label") or agent_id.title())
        runtime = str(definition.get("runtime") or "codex")
        node = str(definition.get("node") or "")
        if not LABEL_RE.match(label) or not NAME_RE.match(runtime) or (node and not NAME_RE.match(node)):
            self.errors.append(f"{agent_id}: label, runtime and node must be short plain names")
            return None
        try:
            remote_port = int(definition.get("remote_port") or 0)
            if not 1 <= remote_port <= 65535:
                raise ValueError("remote_port must be a port number")
            transport = self._transport_factory(definition)
        except (TypeError, ValueError) as exc:
            self.errors.append(f"{agent_id}: {exc}")
            return None
        return Agent(
            agent_id=agent_id, label=label, kind="remote", runtime=runtime, node=node,
            description=str(definition.get("description") or "")[:160],
            console=RemoteConsole(transport, remote_port),
            transport_kind=str(definition.get("transport") or "ssh-loopback"),  # a kind, not an address
            capabilities=capabilities,
        )

    # -- reading -----------------------------------------------------------
    def get(self, agent_id: str) -> Agent:
        agent = self.agents.get(agent_id or "")
        if agent is None:
            raise RemoteError(f"{agent_id!r} is not a configured agent", code="unknown_agent")
        return agent

    def listing(self, *, probe: bool = True) -> dict:
        agents = []
        for agent in self.agents.values():
            if probe and not agent.is_local:
                agent.check()
            agents.append(agent.as_json())
        return {"agents": agents, "default": self.local_id, "errors": self.errors[:5]}

    def close(self) -> None:
        for agent in self.agents.values():
            if agent.console is not None:
                agent.console.close()

    # -- the proxy ---------------------------------------------------------
    def proxy(self, agent: Agent, method: str, suffix: str, raw_query: str, *,
              headers: dict, body: bytes | None) -> ProxiedResponse:
        """Forward exactly one allowlisted console operation to one agent."""

        if agent.is_local or agent.console is None:
            raise RemoteError(f"{agent.id} is served by this console itself",
                              code="not_a_remote_agent")
        check_allowed(method, suffix, raw_query)
        forwarded = {name: headers[name] for name in FORWARDED_REQUEST_HEADERS
                     if headers.get(name)}
        url = suffix + (("?" + raw_query) if raw_query else "")
        timeout = LONG_POLL_TIMEOUT_S if suffix == "/api/events" else REQUEST_TIMEOUT_S
        if method == "POST" and (suffix == "/api/connection/refresh" or
                re.fullmatch(rf"/api/room/{_ROOM}/(?:continue|attach|refresh)", suffix)):
            timeout = CONTROL_TIMEOUT_S
        try:
            status, response_headers, payload = agent.console.request(
                method, url, headers=forwarded, body=body, timeout=timeout)
        except RemoteError as exc:
            if exc.code != "agent_request_timeout":
                agent.note_unavailable(str(exc))
            raise
        agent.note_available()
        content_type = str(response_headers.get("Content-Type")
                           or "application/octet-stream")
        if content_type.split(";")[0].strip().casefold() == "application/json":
            if suffix == "/api/bootstrap":
                # The remote's own CSRF token is this proxy's business alone.
                # The browser keeps using this console's token for everything.
                payload = strip_remote_csrf(payload)
            payload = rewrite_artifact_urls(payload, agent.id)
        extra = tuple((name, response_headers[name]) for name in FORWARDED_RESPONSE_HEADERS
                      if response_headers.get(name))
        return ProxiedResponse(status, payload, content_type, extra)


def check_allowed(method: str, suffix: str, raw_query: str = "") -> None:
    """Refuse anything that is not a named console operation on this agent."""

    if not suffix.startswith("/api/"):
        raise RemoteError("only this console's own API is forwarded to an agent",
                          code="forbidden_path")
    if "%" in suffix or ".." in suffix or "\\" in suffix or "//" in suffix:
        # Room ids and file ids never need an escape; refusing them outright
        # keeps the allowlist below the only thing that decides a path.
        raise RemoteError("that agent path is not a plain console operation",
                          code="forbidden_path")
    if not QUERY_RE.match(raw_query or ""):
        raise RemoteError("that agent request carries an unsupported query",
                          code="forbidden_query")
    # Several entries can describe one path (a room draft is both a room route
    # and its own PUT), so the whole list decides the method, not the first hit.
    matched = False
    for methods, pattern in PROXY_ALLOWLIST:
        if not pattern.fullmatch(suffix):
            continue
        matched = True
        if method in methods:
            return
    if matched:
        raise RemoteError(f"{method} is not forwarded for that agent operation",
                          code="forbidden_method")
    raise RemoteError("that operation is not forwarded to another agent",
                      code="forbidden_path")


def strip_remote_csrf(payload: bytes) -> bytes:
    """Drop a remote console's CSRF token before its boot state is forwarded."""

    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    if not isinstance(parsed, dict) or "csrf" not in parsed:
        return payload
    return json.dumps({key: value for key, value in parsed.items()
                       if key != "csrf"}).encode("utf-8")


def rewrite_artifact_urls(payload: bytes, agent_id: str) -> bytes:
    """Point a remote agent's artifact URLs back at this same-origin console.

    Only whole strings that are exactly one of the console's own artifact paths
    are touched. Message text, native output and every other string is returned
    byte-for-byte as the agent wrote it.
    """

    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    prefix = f"/api/agents/{agent_id}"

    def walk(value):
        if isinstance(value, str):
            return prefix + value if ARTIFACT_URL_RE.match(value) else value
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return json.dumps(walk(parsed)).encode("utf-8")
