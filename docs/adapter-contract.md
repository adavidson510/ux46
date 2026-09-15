# Connect another system

The core supports native Codex and Claude adapters. Other runtimes can extend
UX46 through a separate local adapter; skip native selection during installation.

A model completion endpoint is not enough. A UX46 adapter represents persistent
sessions, their actual lifecycle, file ownership and delivery receipts.
`tools/atlas_remote.py` defines the allowlisted routes and forwarding contract;
`tools/atlas_claude.py` is a working adapter example. Synthetic runtime fixtures
and `tests/test_atlas_remote.py` exercise the boundary.

At minimum, expose a loopback HTTP service with these compatible projections:

| Route | Purpose |
| --- | --- |
| GET /api/bootstrap | CSRF token, node, capabilities and observed runtime state |
| GET /api/session-options | Whether creation is available and explicit project choices |
| GET /api/projects, /api/rooms, /api/workspace | Scoped project/session inventory |
| GET /api/attention, /api/events | Status and bounded event updates |
| POST /api/sessions | Idempotent creation, with exact new room/native ID |
| GET /api/room/{project}/{session} | Actual session state and history metadata |
| POST /api/room/{project}/{session}/submit | Input receipt, never an invented answer |

Use the exact schemas demonstrated by the adapter and fixtures. A simple health
response or compatible chat-completions API is not sufficient. Preserve native
ownership, distinguish accepted input from finished work, and never silently
retry a turn with unknown delivery. Declare unsupported capabilities honestly.

The front verifies its own host, origin and CSRF before proxying mutations. It
fetches the adapter's own bootstrap token for the onward request. Configure exact
trusted loopback ports in the private agents.json. The quick `ux46 connect`
command accepts no arbitrary URL or shell command. Separate secured transports
are advanced configuration, not part of a default installation.
