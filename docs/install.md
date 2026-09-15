# Install UX46

On macOS or Linux (including WSL), paste this into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh | sh
```

The installer downloads a checked release into `~/ux46`, creates private settings
in `~/.ux46`, and opens the workspace in your browser. You do not need Git, a
GitHub account, a server, or a subscription to UX46. Your agent provider's account
and usage terms still apply.

You can paste **that same command into your AI**. The script points it to the
AI installation guide and its local connection record. With captured output,
installation returns control without prompts or a foreground server, so the AI
can finish configuration, verify it, and start the workspace for you. In a normal
terminal it opens the workspace. Use `--start` or `--no-start` to override this.

If Python 3.10+ is missing, the installer obtains a private Python 3.12 using
[Astral uv](https://docs.astral.sh/uv/guides/install-python/). It does not use sudo,
replace system Python, or edit your shell profile.

## Choose an agent

- **Only Codex or Claude is found:** that CLI is selected.
- **Both or neither are found:** choose Codex, Claude, or **Skip / connect my own later**.
- **The selected CLI is missing:** installation finishes and points you to its
  official setup guide. Install it, sign in there, then start UX46.
- **Skip:** the workspace and local Constellation are available. Conversations
  need a configured native agent or compatible adapter.

Official setup guides: [Codex CLI](https://developers.openai.com/codex/cli) and
[Claude Code](https://code.claude.com/docs/en/setup). UX46 never asks you to paste
provider passwords, API keys or authentication files into its installer.

To choose explicitly and install without starting a server:

```sh
curl -fsSL https://raw.githubusercontent.com/adavidson510/ux46/main/install.sh | sh -s -- --agent claude --no-start
```

Use `--agent codex` or `--agent none` instead if preferred.
Without an interactive terminal, ambiguous detection selects none; `--agent auto`
refuses to guess when both CLIs are available.

## Start, stop, and change your agent

```sh
~/.local/bin/ux46 run --open
~/.local/bin/ux46 setup --agent claude --name "My helper"
~/.local/bin/ux46 doctor
```

Stop the foreground server with Ctrl+C. Restart after configuration changes.
If port 8877 is occupied, use `run --port 8878 --open`.
No background service is installed. Your local source remains fully editable.

The command also works as `ux46` if `~/.local/bin` is already on your PATH.
Custom locations use `UX46_INSTALL_DIR`, `UX46_HOME`, and `UX46_BIN_DIR`. Set them
on the `sh` side of a curl pipeline, or download the script before running it.
Use separate absolute directories for source and private data.

Existing source, launchers or configuration are never replaced by reinstalling.
Choose a new location to try a second independent copy. To update a customized
copy, compare it with a newer release and bring across the changes you want.

## Begin fresh

Click the UX46 symbol or **New conversation**. Name conversations as you go.
Register only projects you want in this workspace:

```sh
~/.local/bin/ux46 project-add /path/to/my-project --name "My project"
```

That adds `project.json` if absent. Future records live in the project's
`sessions/` directory; exclude those private records from public commits.
An existing nonempty sessions directory is refused instead of silently imported.
Both standalone adapters begin with explicitly registered or newly created
sessions. They do not sweep old conversations into your new Vault.

## Inspect first, or remove it

Prefer to read the script before running it? Download `install.sh`, inspect it,
and run `sh install.sh`. It verifies the source archive checksum before extraction.
See [security and privacy](../SECURITY.md) for the scope of that protection.

To uninstall, stop UX46 and remove its launcher and source directory. Keep
`~/.ux46` and project `sessions/` folders if you want your settings and memories.
The installer does not alter provider CLI installations or authentication.
