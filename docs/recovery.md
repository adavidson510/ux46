[← UX46](../README.md) · [Architecture](architecture.md) · [AI setup](ai-install.md)

# When the workspace needs help

Start with a check on the machine that runs your installation:

```sh
ux46 doctor --check
ux46 --doctor --check --json
```

Both spellings run the same doctor. A check reads configuration, checks the
selected native command and its prerequisites, and asks reachable adapters for
current state. It does not restart services, sign in, send a model prompt, or
read authentication files. Unknown account or quota state stays unknown. An old
usage-limit failure is history, not proof that the current account is limited.
Use `--agent local --room project/session` to inspect a particular filed room.

**Refresh this agent** is beside the agent name in the workspace. It reconnects
idle owned Codex connections using the login already saved on that agent's
host. Busy, uncertain, and unsupported connections are reported once. The same
operation is available outside the browser:

```sh
ux46 doctor --agent local --recover
```

**Recover UX46** in the workspace menu explicitly allows interruption of this
installation's configured owned work. It holds unsent queued messages, stops
verified owned services in reverse dependency order, starts them in dependency
order, and checks the restored connections. There is no second routine
confirmation. From a terminal, including when the console is down:

```sh
ux46 doctor --recover-all
```

Conversations and drafts stay in their existing stores. Held queued messages
need to be edited and sent when you are ready. Accepted or uncertain input is
never replayed. Recovery does not renew a provider's allowance, transfer a login,
or automatically restart an active goal. A partial result names the unresolved
pieces; it is not reported as complete.

## What runs independently

[The coordinator](../tools/ux46_recovery.py) runs in the CLI process. Browser
requests launch that same CLI as a detached process, outside the console's
lifetime. The response to a restart request can disappear as the console stops;
the browser reads the saved receipt with the original request ID and never
repeats the operation to find out what happened.

Private records live under `$UX46_HOME/control` (default `~/.ux46/control`). A
process lock serializes recovery, and an admission barrier lets existing sends
settle before interruption. A controller that disappears leaves an incomplete
receipt. Reusing its ID reports that interruption without replaying any steps.
The temporary admission barrier expires after the bounded operation window;
held queue rows remain held. A new recovery is a new explicit action.

For scripted callers, `--request-id` accepts a stable ID of 8–64 letters, digits,
underscores or hyphens. The receipt is JSON with `--json`. Exit status zero means
the requested recovery completed; partial recovery has a nonzero exit status.

## Private service and remote-agent mappings

Ordinary `ux46 run` records its process birth, interpreter, source and port. The
coordinator verifies the process birth before signalling it. It does not search
for process names, kill unrelated native apps, or force termination after an
uncertain graceful stop.

Additional services are opt-in entries in private `$UX46_HOME/recovery.json`:

```json
{
  "schema_version": 1,
  "services": [
    {
      "id": "adapter",
      "manager": "systemd",
      "target": "ux46-adapter.service",
      "definition": "/absolute/private/path/ux46-adapter.service",
      "depends_on": [],
      "agents": ["remote"],
      "endpoint": {"port": 8878}
    }
  ],
  "agents": [
    {"id": "remote", "runtime": "codex", "endpoint": {"port": 8878}}
  ]
}
```

`launchd` mappings instead use an explicit `gui/UID/LABEL` target and its owner-owned
plist. Both managers verify the loaded service's definition against the supplied
file. An `agents` list is required on every mapped service; use an empty list
for a gateway with no native adapter. Missing or cyclic dependencies are refused.
Mapped HTTP endpoints are loopback ports, optionally with configured `Host` and
`Tailscale-User-Login` headers. Never put provider credentials in this mapping.

Each adapter must opt into its own private recovery root and advertise input
admission before connection replacement is attempted. An older adapter without
that capability is reported as unsupported, and its associated service is not
stopped. Remote service management without a locally owned manager mapping is
also reported as unsupported. The first version does not SSH to hosts or infer
ownership from a process list.

The optional access gateway accepts `--recovery-root /absolute/private/root`.
Its recovery status and start routes use the same coordinator even when the
upstream console cannot answer. Mutation requires the existing signed gateway
login, matching origin and a separate recovery CSRF token. Clients can select
only the mode, configured agent ID and request ID; they cannot supply a command,
PID or filesystem path. Configure independent gateway UI assets to keep the
workspace shell available during a console outage. The terminal route remains
available if the gateway itself is down.

## Current verification boundary

Synthetic native adapters test idle, busy, stopped-error, unknown-state and
lost-response outcomes. Browser journeys test fixed targets, draft preservation
and the explicit interruption action. Temporary no-provider process fixtures
on macOS exercise a dead console and a console that stops during its own browser
request, including duplicate receipts and an unrelated process left untouched.

This does not establish live recovery for every provider or service manager.
Claude connection refresh is currently reported as unsupported; service recovery
still reports its separate result. Linux user-systemd and additional private
host mappings need their own installation-level verification. A successful
health check is not a completed native coding task or an outside-user pilot.
