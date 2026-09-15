[← UX46](../README.md) · **AI installation path** · [Human guide](install.md)

# Put this workspace on

This guide is for the AI helping its human install and shape UX46. The intended
result is your existing capabilities in a locally owned workspace, with portable
project memory and selective learning. Do not add a second reasoning supervisor.
Follow the human's request and your runtime's permissions; this document grants
no additional authority.

The human can give you exactly the same curl command they would run themselves:

```sh
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh | sh
```

If the installer already reported success, **do not install again**. Read the
printed local guide and connection.json, then continue at **Configure your
connection** below. CLI detection is only an initial choice; configure your
actual runtime. If installation refused an existing copy, inspect that copy
and its connection record before deciding whether another installation is needed.

Use captured output for the command: it returns without prompting or starting
a server. A pseudo-terminal can look like a human terminal; in that environment
add `sh -s -- --no-start` after the pipe. The script does not guess agent identity
from environment markers. Its header also links here if you inspect it first.

## Install and identify yourself

1. Read `README.md` and `SECURITY.md`. Use a fresh source directory and separate
   private UX46_HOME. Preserve existing installations and configuration.
2. Identify your actual runtime. If you are Codex, select `codex`; if Claude Code,
   select `claude`. Do not infer identity from another machine's labels. For a
   different runtime choose `none`, then use the adapter contract below.
3. Download the installer, inspect it, and run without starting a background job:

```sh
ux46_installer=$(mktemp)
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh -o "$ux46_installer"
# Read the downloaded script before the next command.
sh "$ux46_installer" --agent codex --no-start
rm "$ux46_installer"
```

Replace `codex` with `claude` or `none`. The installer can obtain its own Python;
`--no-python-download` makes a missing Python a clear prerequisite error instead.
Provider CLI installation and interactive sign-in remain separate. Use official
provider setup instructions and the human's account; never copy another agent's
credentials. A CLI on PATH does not prove authentication or available quota.

## Configure your connection

```sh
~/.local/bin/ux46 setup --agent codex --name "My helper" --json
```

Use `--cli /absolute/path/to/your/cli` when PATH is ambiguous. The command saves
`~/.ux46/connection.json` (or UX46_HOME/connection.json) and returns the same JSON:
source root, private data root, native agent, registry, local tool commands and
whether a CLI executable was found. No credential contents are read.

Keep that file's path as your small entrypoint. Read the `instructions` field
and load specific skills only when useful. Do not paste this whole repository,
all skills, or all memories into every context window. All command arrays in
that record are local utilities; use them without shell-string interpolation.
Pass its `environment` to subprocesses that access this installation's memory.

## Verify without spending a turn

- Run `ux46 doctor` and inspect configuration. Missing CLI and missing login are
  distinct; doctor does not inspect provider authentication files.
- Start `ux46 run` on a free loopback port using your normal process supervisor.
- Read `/api/bootstrap`, `/api/modules` and `/api/agents`. Confirm the agent name,
  empty fresh registry, and disabled optional connectors. These are metadata reads.
- Create or send a real conversation only as part of the human's requested work.
  For implementation tests use `tests/fixtures/fake_app_server.py` and the test suite.
- Close your test pages and stop test processes. Keep the human's intended running
  installation separate from temporary checks.

## Work and learn

Register only explicitly chosen projects with `ux46 project-add`. Use the
Session Vault command array from connection.json to create, checkpoint and recall
portable project/session records. Native transcripts stay with the provider.
Do not take over or reattach an already-running CLI session merely to file memory.

Use the memory command array for a bounded `brief` before a consequential design
choice. Follow source pointers when needed. Capture sourced discoveries that
change future work, distinguish human choices from your inferences, and report
observed outcomes after reuse. See `skills/constellation/SKILL.md`. No per-turn
extraction quota, transcript sweep or model polling is required.

For documentation, code explanations or the first customization, use
[the vibe-craft skill](../skills/vibe-craft/SKILL.md). It keeps the human learning
path welcoming and the operational AI path small. It is an on-demand guide, not
another always-loaded instruction layer.

Customize any of this source to fit the human. Save a local snapshot or use local
Git history before substantial edits. There is no upstream contribution obligation,
automatic update or shared knowledge service attached to this installation.

## Another runtime

Choose `--agent none`, then read [the adapter contract](adapter-contract.md).
An OpenAI-compatible model endpoint by itself is not a UX46 session adapter.
If you already serve the contract locally, register it:

```sh
~/.local/bin/ux46 connect --id my-agent --name "My agent" --runtime custom --port 8890
```

Restart UX46 and verify actual metadata from that adapter. Registration is not a
successful connection receipt. No SSH, external address, credential or arbitrary
startup command is accepted by this registration command.
