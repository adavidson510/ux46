# How much can your AI reach?

Start with **Project access**. Your AI can work in the project's folder and
use temporary files. When a command needs broader access, Codex can ask you.
It can still read files outside the project; this is a limit on writes, not a
private container for your files.

When you're ready, open a conversation and choose **Room settings → Connection**.
Under your agent's **access default**, choose:

| Choice | What it means |
| --- | --- |
| **Project access** | Work in the project and temporary folders; request approval for broader commands or network access. |
| **Full access (YOLO)** | Use files, commands and network available to your OS account, without routine approval prompts. Choose this when you're comfortable letting the agent act with your account's reach. |
| **Keep CLI / session settings** | Let new conversations use your CLI defaults and existing ones keep their saved permissions. |

Click **Save default**. That sets the choice for new connections. To apply it
to the conversation you're already using, wait for its work to finish and click
**Reconnect** below. Your conversation stays put. No new chapter, copied history,
or repeated message is needed.

The **This session** line shows what the runtime actually granted. If it says
access has not been verified, that is not a claim that your choice took effect.

Full access does not turn your account into an administrator or give it someone
else's credentials. It does let the agent change files your account can change,
including outside the project. Your instructions still matter, but instructions
are not the same thing as an enforced filesystem restriction.

Fresh Codex installations start with Project access. Existing configurations
keep their previous behavior until you choose a default. This control currently
covers Codex; Claude Code uses its own permission mode and starts in its native
`default` mode.

## For your AI

The Codex console serves `GET /api/execution-policy` and authenticated,
Origin/CSRF-checked `POST /api/execution-policy` with `{policy, base_revision}`.
Values: `workspace-write`, `full-access`, `preserve`. Store lives at
`$UX46_HOME/state/execution-policy.json`; a saved choice overrides the launch
setting. Initial configuration uses `execution_policy` in
`$UX46_HOME/config.json`. Do not overwrite an existing choice during setup.
Read effective `native.execution_profile` after reconnect; never treat the
requested default as proof of a grant. Do not send a test prompt to verify this.
