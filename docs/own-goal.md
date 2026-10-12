[← Architecture](architecture.md)

# Ask a conversation to resume its own goal

With the optional helper installed, tell a Codex room “resume your saved goal.”
Its agent requests the existing UX46 Resume action for that same conversation.
If the agent is still answering, a small local process waits for that reply to
finish. It never interrupts the reply. The existing goal badge continues to
show native status; waiting does not make a blocked goal look active.

The agent reports that the request is waiting, then ends its reply. Once the
native runtime confirms active, normal goal continuation can proceed. This
adds no model polling, new chat message, or dismissible completion card.
Resumed work still uses the room's normal model allowance.

## Local setup

This alpha helper is for Codex rooms served by an already configured UX46
console on the same host. It is not automatically enabled on remote machines
or Claude adapters. Install `skills/ux46-own-goal` in the local agent's skills
and expose this command as `ux46-own-goal`:

```sh
python3 /path/to/ux46/tools/ux46_own_goal.py --directory /path/to/private-data
```

Configure private `goal-host.json` in that data directory using the existing
console's authenticated loopback settings. Do not put it in the source checkout:

```json
{
  "agent": "local",
  "console": "http://127.0.0.1:8877",
  "origin": "https://workspace.example.com",
  "user": "user@example.com"
}
```

Use the actual registered local Codex adapter ID and the console's existing
trusted-origin/user configuration. This reuses its authentication boundary;
it does not create one or configure the console to trust a new identity.
For a simple local-only console, use its loopback origin and an empty user.
No API keys, new model subscription, gateway reload or native restart is needed.

Run `ux46-own-goal status` from a connected Codex conversation to inspect its
own receipt. Requests derive identity from `CODEX_THREAD_ID`; there is no room
or thread selector. The helper has `resume --request '<exact human wording>'`,
`status`, and `cancel` actions. Properly quote arguments; never paste unescaped
human wording into shell code.

## What is checked

The exact current human message, unique canonical room, native thread, saved
goal identity and budget are retained. The helper checks them again before
using `/goal resume`; that existing action enforces native writer ownership
and idle/approval admission. It sends no objective, budget, permission change,
new turn or interrupt. A native result must confirm active before the receipt
says resumed. `status` also reads current goal state independently of the receipt.

A newer human message cancels pending direction. Changed/unavailable targets,
replaced or completed goals, disconnected workers, and unknown reads stop it.
Only active, blocked and paused saved goals are eligible; this helper never
resets usage limits. A definite busy refusal can wait. A crash during dispatch
or a lost response is unconfirmed and is never automatically replayed.
Duplicate calls for the same human message return the same receipt.

Waiting lasts at most 30 minutes and requires the host to remain awake. The
helper survives the agent command ending and does not require an open browser.
It is not a system service: logout/reboot can stop it. `status` detects a stopped
waiter after 90 seconds; use a fresh human request or the existing Resume button.

This first slice accepts direct human requests only, not automatic packets or
assistant-written relays. Text equality establishes where a request came from,
not what it authorizes: the agent must interpret explicit direction and obey
its runtime instructions. An unrestricted OS-account process can alter its own
environment/files; this convenience scope is not a security sandbox.

## Verification

Controlled tests use disposable goal state and the fake native app-server,
including a real local HTTP console journey. They check waiting, same-goal
resumption, unchanged budget/counters, unrelated worker preservation, duplicates,
cancellation, replaced identities, approvals, limits and uncertain outcomes.
No unrelated live goal is resumed to test this feature.

The native operation uses the documented
[Codex goal API](https://learn.chatgpt.com/docs/app-server#manage-a-thread-goal).
